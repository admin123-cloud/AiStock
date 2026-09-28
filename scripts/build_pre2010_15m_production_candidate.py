from __future__ import annotations

"""Build an isolated, versioned 15m production-replacement candidate.

The candidate is intentionally separate from all formal minute tables.  For a
complete code-day the purchased native 15m rows win.  If that code-day is not
complete, a complete 5m code-day is aggregated into 15m bars.  Unresolved
code-days are retained in the audit table and are never represented by a
partial 15m candidate.
"""

import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_client

TZ = ZoneInfo("Asia/Shanghai")
SCOPE = (int(os.getenv("AISTOCK_NATIVE15_START_YEAR", "2000")), int(os.getenv("AISTOCK_NATIVE15_END_YEAR", "2009")))
RUN_ID = os.getenv("AISTOCK_NATIVE15_RUN_ID", "purchased_native_15_pre2010_v20260921_candidate")
OUT = Path(r"F:\AiStock\data\data\runtime") / RUN_ID
CANDIDATE = f"stock.{RUN_ID}"
DAY_AUDIT = f"stock.{RUN_ID}_day_audit"
YEAR_AUDIT = f"stock.{RUN_ID}_year_audit"

NATIVE = os.getenv("AISTOCK_NATIVE15_NATIVE", "stock.wdz_native_15_pre2010_v20260921_stage")
FIVE = os.getenv("AISTOCK_NATIVE15_FIVE", "stock.kline_minute_5_pre2010_v20260921_r2_fallback_highvol")
DAILY = os.getenv("AISTOCK_NATIVE15_DAILY", "stock.kline_daily_pre2010_source_v20260921")

SETTINGS = "SETTINGS max_threads=1, max_memory_usage=10000000000, max_bytes_before_external_group_by=1000000000, max_bytes_before_external_sort=1000000000"


def exists(c, table: str) -> bool:
    db, name = table.split(".", 1)
    rows = c.query(
        "SELECT count() FROM system.tables WHERE database=%(db)s AND name=%(name)s",
        parameters={"db": db, "name": name},
    ).result_rows
    return bool(rows and int(rows[0][0]))


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
            fh.write("\n")
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def stable_id(code: str, dt: datetime) -> int:
    return int.from_bytes(hashlib.blake2b(f"{code}|{dt:%Y-%m-%d %H:%M:%S}|15m|{RUN_ID}".encode(), digest_size=8).digest(), "big")


def ddl() -> tuple[str, str, str, str]:
    candidate = f"""CREATE TABLE {CANDIDATE}
    (
      code String, datetime DateTime('Asia/Shanghai'),
      open Float64, high Float64, low Float64, close Float64,
      volume Float64, amount Float64,
      volume_unit LowCardinality(String), amount_unit LowCardinality(String),
      source_kind LowCardinality(String), source_priority UInt8, provenance String,
      source_frequency LowCardinality(String), source_year UInt16, trade_date Date,
      input_bars UInt16, quality_status LowCardinality(String), batch_id String,
      created_at DateTime('Asia/Shanghai'), id UInt64
    ) ENGINE = MergeTree PARTITION BY toYYYYMM(datetime)
    ORDER BY (code, datetime) SETTINGS index_granularity=8192"""
    day_status = f"""CREATE TABLE {DAY_AUDIT}_status
    (
      source_year UInt16, code String, trade_date Date,
      native_rows UInt32, native_complete UInt8,
      five_rows UInt32, five_complete UInt8,
      chosen_source LowCardinality(String), chosen_priority UInt8,
      chosen_bars UInt16, review_reason LowCardinality(String),
      batch_id String, created_at DateTime('Asia/Shanghai')
    ) ENGINE = MergeTree ORDER BY (source_year, code, trade_date)"""
    day_audit = f"""CREATE TABLE {DAY_AUDIT}
    (
      source_year UInt16, code String, trade_date Date,
      native_rows UInt32, native_complete UInt8,
      five_rows UInt32, five_complete UInt8,
      chosen_source LowCardinality(String), chosen_priority UInt8,
      chosen_bars UInt16, review_reason LowCardinality(String),
      daily_exists UInt8, candidate_bars UInt16, candidate_amount Float64,
      daily_amount Float64, amount_relative_diff Float64,
      price_relative_diff Float64, volume_relative_diff Float64,
      audit_status LowCardinality(String), batch_id String,
      created_at DateTime('Asia/Shanghai')
    ) ENGINE = MergeTree ORDER BY (source_year, code, trade_date)"""
    year_audit = f"""CREATE TABLE {YEAR_AUDIT}
    (
      source_year UInt16, native_code_days UInt64, five_code_days UInt64,
      native_complete_days UInt64, five_complete_days UInt64,
      native_selected_rows UInt64, five_selected_rows UInt64,
      review_code_days UInt64, candidate_rows UInt64, candidate_code_days UInt64,
      daily_overlap_code_days UInt64, daily_amount_over_1pct UInt64,
      daily_price_over_0_5pct UInt64, daily_volume_over_0_1pct UInt64,
      created_at DateTime('Asia/Shanghai'), batch_id String
    ) ENGINE = MergeTree ORDER BY source_year"""
    return candidate, day_status, day_audit, year_audit


def native_day_stats(year: int) -> str:
    # The stage audit already identifies the incomplete members, but repeat the
    # logical code-day check here so the candidate has an independent gate.
    return f"""
      SELECT code, toDate(datetime) AS trade_date,
        count() AS native_rows,
        toUInt8(count()=16 AND uniqExact(datetime)=16
          AND min(if(toHour(datetime)<12,
                     intDiv(dateDiff('minute',toStartOfDay(datetime)+toIntervalHour(9)+toIntervalMinute(30),datetime),15)-1,
                     8+intDiv(dateDiff('minute',toStartOfDay(datetime)+toIntervalHour(13),datetime),15)-1))=0
          AND max(if(toHour(datetime)<12,
                     intDiv(dateDiff('minute',toStartOfDay(datetime)+toIntervalHour(9)+toIntervalMinute(30),datetime),15)-1,
                     8+intDiv(dateDiff('minute',toStartOfDay(datetime)+toIntervalHour(13),datetime),15)-1))=15
          AND uniqExact(if(toHour(datetime)<12,
                     intDiv(dateDiff('minute',toStartOfDay(datetime)+toIntervalHour(9)+toIntervalMinute(30),datetime),15)-1,
                     8+intDiv(dateDiff('minute',toStartOfDay(datetime)+toIntervalHour(13),datetime),15)-1))=16) AS native_complete
      FROM {NATIVE}
      WHERE source_year=%(year)s
      GROUP BY code, trade_date
    """


def five_day_stats(year: int) -> str:
    return f"""
      SELECT code, toDate(datetime) AS trade_date,
        count() AS five_rows,
        toUInt8(count()=48 AND uniqExact(datetime)=48
          AND min(if(toHour(datetime)<12,
                     intDiv(dateDiff('minute',toStartOfDay(datetime)+toIntervalHour(9)+toIntervalMinute(30),datetime),5)-1,
                     24+intDiv(dateDiff('minute',toStartOfDay(datetime)+toIntervalHour(13),datetime),5)-1))=0
          AND max(if(toHour(datetime)<12,
                     intDiv(dateDiff('minute',toStartOfDay(datetime)+toIntervalHour(9)+toIntervalMinute(30),datetime),5)-1,
                     24+intDiv(dateDiff('minute',toStartOfDay(datetime)+toIntervalHour(13),datetime),5)-1))=47
          AND uniqExact(if(toHour(datetime)<12,
                     intDiv(dateDiff('minute',toStartOfDay(datetime)+toIntervalHour(9)+toIntervalMinute(30),datetime),5)-1,
                     24+intDiv(dateDiff('minute',toStartOfDay(datetime)+toIntervalHour(13),datetime),5)-1))=48) AS five_complete
      FROM {FIVE}
      WHERE toYear(datetime)=%(year)s
      GROUP BY code, trade_date
    """


def main() -> int:
    c = clickhouse_client()
    candidate_ddl, day_status_ddl, day_audit_ddl, year_audit_ddl = ddl()
    day_status = f"{DAY_AUDIT}_status"
    # Never reuse or append to an old candidate.  A rerun must use a new run id.
    for table in (CANDIDATE, day_status, DAY_AUDIT, YEAR_AUDIT):
        if exists(c, table):
            raise SystemExit(f"refusing existing isolated candidate table: {table}")
    c.command(candidate_ddl)
    c.command(day_status_ddl)
    c.command(day_audit_ddl)
    c.command(year_audit_ddl)

    OUT.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": "purchased-native-15m-production-candidate-v1",
        "run_id": RUN_ID, "scope": list(SCOPE),
        "native_source": NATIVE, "fallback_5m_source": FIVE,
        "daily_source": DAILY, "candidate_table": CANDIDATE,
        "day_status_table": day_status, "day_audit_table": DAY_AUDIT,
        "year_audit_table": YEAR_AUDIT, "formal_tables_touched": False,
        "formal_mutation": False, "status": "running", "years": {},
        "started_at": datetime.now(TZ).isoformat(),
        "memory_contract": {"max_memory_usage": 10000000000, "max_threads": 1},
        "source_precedence": ["native_purchased_15m_complete", "aggregate_5m_complete", "aggregate_1m_complete_review_only"],
    }
    write_json(OUT / "manifest.json", manifest)

    for year in range(SCOPE[0], SCOPE[1] + 1):
        params = {"year": year}
        status_sql = f"""
        INSERT INTO {day_status}
        (source_year, code, trade_date, native_rows, native_complete,
         five_rows, five_complete, chosen_source, chosen_priority,
         chosen_bars, review_reason, batch_id, created_at)
        SELECT toUInt16(%(year)s), if(n.code!='', n.code, f.code),
          if(n.code!='', n.trade_date, f.trade_date),
          if(n.code!='', n.native_rows, 0), if(n.code!='', n.native_complete, 0),
          if(f.code!='', f.five_rows, 0), if(f.code!='', f.five_complete, 0),
          multiIf(n.native_complete=1, 'native_purchased_15m',
                  f.five_complete=1, 'aggregate_5m', 'review'),
          multiIf(n.native_complete=1, toUInt8(1), f.five_complete=1, toUInt8(2), toUInt8(0)),
          multiIf(n.native_complete=1, toUInt16(1), f.five_complete=1, toUInt16(3), toUInt16(0)),
          multiIf(n.native_complete=1, 'native_complete', f.five_complete=1, 'native_incomplete_5m_complete', 'no_complete_5m_or_native'),
          %(batch)s, now('Asia/Shanghai')
        FROM ({native_day_stats(year)}) AS n
        FULL OUTER JOIN ({five_day_stats(year)}) AS f
          ON n.code=f.code AND n.trade_date=f.trade_date
        {SETTINGS}
        """
        c.query(status_sql, parameters={"year": year, "batch": RUN_ID})

        native_insert = f"""
        INSERT INTO {CANDIDATE}
        (code, datetime, open, high, low, close, volume, amount,
         volume_unit, amount_unit, source_kind, source_priority, provenance,
         source_frequency, source_year, trade_date, input_bars,
         quality_status, batch_id, created_at, id)
        SELECT n.code, n.datetime, n.open, n.high, n.low, n.close,
          n.volume, n.amount, n.volume_unit, n.amount_unit,
          'native_purchased_15m', toUInt8(1),
          concat(n.source_file, '|', n.source_member, '|', n.batch_id),
          '15m', n.source_year, toDate(n.datetime), toUInt16(1),
          'native_complete', %(batch)s, now('Asia/Shanghai'),
          cityHash64(n.code, toString(n.datetime), '15m', %(batch)s)
        FROM {NATIVE} AS n
        INNER JOIN
          (SELECT code, trade_date FROM {day_status}
           WHERE source_year=%(year)s AND chosen_source='native_purchased_15m') AS s
          ON n.code=s.code AND toDate(n.datetime)=s.trade_date
        WHERE n.source_year=%(year)s
        {SETTINGS}
        """
        c.query(native_insert, parameters={"year": year, "batch": RUN_ID})

        # 5m timestamps are closing timestamps.  The expression below maps
        # 09:35..11:30 and 13:05..15:00 into 16 closing 15m buckets.
        five_insert = f"""
        INSERT INTO {CANDIDATE}
        (code, datetime, open, high, low, close, volume, amount,
         volume_unit, amount_unit, source_kind, source_priority, provenance,
         source_frequency, source_year, trade_date, input_bars,
         quality_status, batch_id, created_at, id)
        SELECT a.code, a.bucket, a.open, a.high, a.low, a.close,
          a.volume, a.amount, 'lots', 'yuan', 'aggregate_5m', toUInt8(2),
          concat('{FIVE}', '|', toString(a.code), '|', toString(toDate(a.bucket))),
          '15m', toUInt16(%(year)s), toDate(a.bucket), toUInt16(3),
          'five_complete_fallback', %(batch)s, now('Asia/Shanghai'),
          cityHash64(a.code, toString(a.bucket), '15m', %(batch)s)
        FROM
        (
          SELECT code, bucket,
            argMin(open, datetime) AS open, max(high) AS high,
            min(low) AS low, argMax(close, datetime) AS close,
            sum(volume) AS volume, sum(amount) AS amount,
            count() AS source_bars, uniqExact(datetime) AS unique_source_bars
          FROM
          (
            SELECT code, datetime, open, high, low, close, volume, amount,
              if(toHour(datetime)<12,
                 toStartOfDay(datetime)+toIntervalHour(9)+toIntervalMinute(30)
                   +toIntervalMinute(if(dateDiff('minute',toStartOfDay(datetime)+toIntervalHour(9)+toIntervalMinute(30),datetime)<=0,15,
                       intDiv(dateDiff('minute',toStartOfDay(datetime)+toIntervalHour(9)+toIntervalMinute(30),datetime)+14,15)*15)),
                 toStartOfDay(datetime)+toIntervalHour(13)
                   +toIntervalMinute(if(dateDiff('minute',toStartOfDay(datetime)+toIntervalHour(13),datetime)<=0,15,
                       intDiv(dateDiff('minute',toStartOfDay(datetime)+toIntervalHour(13),datetime)+14,15)*15))) AS bucket
            FROM {FIVE}
            WHERE toYear(datetime)=%(year)s
          ) AS x
          GROUP BY code, bucket
          HAVING source_bars=3 AND unique_source_bars=3
        ) AS a
        INNER JOIN
          (SELECT code, trade_date FROM {day_status}
           WHERE source_year=%(year)s AND chosen_source='aggregate_5m') AS s
          ON a.code=s.code AND toDate(a.bucket)=s.trade_date
        {SETTINGS}
        """
        c.query(five_insert, parameters={"year": year, "batch": RUN_ID})

        counts = c.query(
            f"""SELECT
              (SELECT count() FROM {day_status} WHERE source_year=%(year)s) AS status_days,
              (SELECT count() FROM {day_status} WHERE source_year=%(year)s AND chosen_source='native_purchased_15m') AS native_days,
              (SELECT count() FROM {day_status} WHERE source_year=%(year)s AND chosen_source='aggregate_5m') AS five_days,
              (SELECT count() FROM {day_status} WHERE source_year=%(year)s AND chosen_source='review') AS review_days,
              (SELECT count() FROM {CANDIDATE} WHERE source_year=%(year)s) AS candidate_rows,
              (SELECT uniqExact(tuple(code,trade_date)) FROM {CANDIDATE} WHERE source_year=%(year)s) AS candidate_days
              {SETTINGS}""", parameters={"year": year}
        ).result_rows[0]
        record = {
            "status_days": int(counts[0]), "native_days": int(counts[1]),
            "five_days": int(counts[2]), "review_days": int(counts[3]),
            "candidate_rows": int(counts[4]), "candidate_days": int(counts[5]),
            "status": "pass" if int(counts[3]) == 0 else "review",
        }
        manifest["years"][str(year)] = record
        manifest["updated_at"] = datetime.now(TZ).isoformat()
        write_json(OUT / "manifest.json", manifest)
        print(json.dumps({"year": year, **record}, ensure_ascii=False), flush=True)

    # Candidate-level structural and daily reconciliation audit.  This is
    # inserted only after all source-priority decisions are materialized.
    recon_sql = f"""
    INSERT INTO {DAY_AUDIT}
    (source_year, code, trade_date, native_rows, native_complete,
     five_rows, five_complete, chosen_source, chosen_priority, chosen_bars,
     review_reason, daily_exists, candidate_bars, candidate_amount,
     daily_amount, amount_relative_diff, price_relative_diff,
     volume_relative_diff, audit_status, batch_id, created_at)
    SELECT s.source_year, s.code, s.trade_date, s.native_rows, s.native_complete,
      s.five_rows, s.five_complete, s.chosen_source, s.chosen_priority,
      s.chosen_bars, s.review_reason,
      toUInt8(d.code!=''), ifNull(c.candidate_bars,0), ifNull(c.candidate_amount,0),
      ifNull(d.amount,0),
      if(d.code='' OR d.amount=0 OR c.candidate_bars=0, 0., abs(c.candidate_amount-d.amount)/d.amount),
      if(d.code='' OR c.candidate_bars=0 OR d.close=0, 0., abs(c.candidate_close-d.close)/d.close),
      if(d.code='' OR d.volume=0 OR c.candidate_bars=0, 0., abs(c.candidate_volume-d.volume)/d.volume),
      multiIf(s.chosen_source='review','review', d.code='', 'review_no_daily',
              c.candidate_bars=16 AND abs(c.candidate_amount-d.amount)/greatest(d.amount,1.)<=0.01
                AND abs(c.candidate_close-d.close)/greatest(d.close,1.)<=0.005
                AND abs(c.candidate_volume-d.volume)/greatest(d.volume,1.)<=0.001, 'pass', 'review_daily_diff'),
      %(batch)s, now('Asia/Shanghai')
    FROM {day_status} s
    LEFT JOIN
      (SELECT code, trade_date, count() candidate_bars,
              sum(amount) candidate_amount, sum(volume) candidate_volume,
              argMax(close, datetime) candidate_close
       FROM {CANDIDATE}
       GROUP BY code, trade_date) c ON s.code=c.code AND s.trade_date=c.trade_date
    LEFT JOIN {DAILY} d ON s.code=d.code AND s.trade_date=d.trade_date
    WHERE s.source_year=%(year)s
    {SETTINGS}
    """
    for year in range(SCOPE[0], SCOPE[1] + 1):
        c.query(recon_sql, parameters={"year": year, "batch": RUN_ID})
        print(json.dumps({"year": year, "phase": "daily_reconciliation_inserted"}, ensure_ascii=False), flush=True)

    for year in range(SCOPE[0], SCOPE[1] + 1):
        q = f"""
        SELECT %(year)s,
          countIf(native_rows>0), countIf(five_rows>0),
          countIf(native_complete=1), countIf(five_complete=1),
          countIf(chosen_source='native_purchased_15m')*16,
          countIf(chosen_source='aggregate_5m')*16,
          countIf(chosen_source='review'),
          countIf(audit_status!='review')*16,
          countIf(audit_status!='review'), countIf(daily_exists=1),
          countIf(daily_exists=1 AND amount_relative_diff>0.01),
          countIf(daily_exists=1 AND price_relative_diff>0.005),
          countIf(daily_exists=1 AND volume_relative_diff>0.001),
          now('Asia/Shanghai'), %(batch)s
        FROM {DAY_AUDIT} WHERE source_year=%(year)s
        {SETTINGS}
        """
        row = c.query(q, parameters={"year": year, "batch": RUN_ID}).result_rows[0]
        c.insert(YEAR_AUDIT, [tuple(row)], column_names=[
            "source_year", "native_code_days", "five_code_days", "native_complete_days", "five_complete_days",
            "native_selected_rows", "five_selected_rows", "review_code_days", "candidate_rows", "candidate_code_days",
            "daily_overlap_code_days", "daily_amount_over_1pct", "daily_price_over_0_5pct", "daily_volume_over_0_1pct",
            "created_at", "batch_id",
        ])

    final = c.query(
        f"SELECT count(), uniqExact(tuple(code,datetime)) FROM {CANDIDATE} {SETTINGS}"
    ).result_rows[0]
    review = c.query(
        f"SELECT count() FROM {DAY_AUDIT} WHERE audit_status LIKE 'review%' {SETTINGS}"
    ).result_rows[0][0]
    manifest.update({
        "status": "complete", "finished_at": datetime.now(TZ).isoformat(),
        "candidate_rows": int(final[0]), "candidate_unique_keys": int(final[1]),
        "audit_rows": int(c.query(f"SELECT count() FROM {DAY_AUDIT}").result_rows[0][0]),
        "review_rows": int(review), "formal_tables_touched": False,
    })
    write_json(OUT / "manifest.json", manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


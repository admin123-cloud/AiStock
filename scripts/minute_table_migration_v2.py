"""Minute-table v2 migration: monthly-partitioned 15m/30m/60m official tables.

Implements the agreed plan:
  Phase 0  baseline   - freeze immutable 5m manifest (run_id/fingerprint/cutoff)
  Phase 1  repair     - 60m is corrupted across all history: full rebuild from 5m
  Phase 2  build      - per-month build of kline_minute_{15,30,60}_v2
                        (60m: aggregate from frozen 5m; 15m/30m: copy from old table)
  Phase 3  exchange   - verify final summaries, then EXCHANGE TABLES atomically
  Phase 4  candidate  - month-scoped candidate tables + REPLACE PARTITION publish

All identifiers are validated; every mutating step re-checks the 5m baseline
fingerprint and refuses to continue when the source has drifted.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.market_warehouse import clickhouse_client  # noqa: E402
from utils.paths import runtime_path  # noqa: E402

PERIOD_MINUTES = {"15": 15, "30": 30, "60": 60}
SOURCE_TABLE = "kline_minute_5"
V2_COLUMNS = "code, datetime, open, high, low, close, volume, amount, created_at, id"
HASH_EXPR = "cityHash64(ifNull(code,''), toString(datetime), toString(open), toString(high), toString(low), toString(close), toString(volume), toString(amount))"

VALID_5M_TIMES = ",".join(
    f"'{hour:02d}:{minute:02d}'"
    for start_hour, start_minute, end_hour, end_minute in ((9, 30, 11, 30), (13, 0, 15, 0))
    for hour, minute in (
        divmod(total, 60)
        for total in range(start_hour * 60 + start_minute + 5, end_hour * 60 + end_minute + 1, 5)
    )
)

V2_DDL = """
CREATE TABLE {table}
(
  code Nullable(String),
  datetime Nullable(DateTime),
  open Nullable(Float64),
  high Nullable(Float64),
  low Nullable(Float64),
  close Nullable(Float64),
  volume Nullable(Float64),
  amount Nullable(Float64),
  created_at Nullable(DateTime),
  id UInt64
)
ENGINE = ReplacingMergeTree
PARTITION BY toYYYYMM(datetime)
ORDER BY (code, datetime)
SETTINGS allow_nullable_key = 1, index_granularity = 8192
"""


def quote_identifier(value: str) -> str:
    if not value.replace("_", "").isalnum() or value[0].isdigit():
        raise ValueError(f"unsafe identifier: {value}")
    return value


def safe_month(value: str) -> str:
    if len(value) != 6 or not value.isdigit() or not (1 <= int(value[4:6]) <= 12):
        raise ValueError(f"unsafe month: {value}")
    return value


def manifest_dir() -> Path:
    path = runtime_path() / "minute_migration_v2"
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_run_id(value: str) -> str:
    if not value or not value.isascii() or not value.replace("_", "").isalnum():
        raise ValueError(f"unsafe run id: {value}")
    return value


def manifest_path(run_id: str) -> Path:
    return manifest_dir() / f"baseline_{safe_run_id(run_id)}.json"


def state_path(kind: str, period: str, month: str, run_id: str) -> Path:
    return manifest_dir() / f"{kind}_{safe_run_id(period)}_{safe_month(month)}_{safe_run_id(run_id)}.json"


def write_state(kind: str, period: str, month: str, run_id: str, **payload) -> None:
    state_path(kind, period, month, run_id).write_text(
        json.dumps({"kind": kind, "period": period, "month": month, "run_id": run_id, **payload},
                   indent=2, default=str), encoding="utf-8"
    )


def require_freeze() -> None:
    if os.getenv("AISTOCK_MINUTE_MIGRATION_FREEZE") != "1":
        raise SystemExit(
            "migration freeze is required: stop minute writers and set "
            "AISTOCK_MINUTE_MIGRATION_FREEZE=1"
        )


def table_schema(client, table: str):
    return client.query(
        "SELECT engine, partition_key, sorting_key FROM system.tables "
        "WHERE database='stock' AND name=%(name)s", parameters={"name": table}
    ).result_rows


def source_cutoff_clause(manifest: dict) -> str:
    cutoff = manifest.get("cutoff")
    if not cutoff:
        return ""
    safe = str(cutoff).replace("'", "''")
    return f" AND (created_at IS NULL OR created_at <= toDateTime('{safe}'))"


def daily_count_file(run_id: str, month: str) -> Path:
    return manifest_dir() / f"daily_counts_{run_id}_{month}.tsv.gz"


def write_daily_counts(client, run_id: str, month: str, cutoff_clause: str) -> dict:
    rows = client.query(
        f"SELECT toDate(datetime), code, count() FROM {SOURCE_TABLE} FINAL "
        f"WHERE toYYYYMM(datetime) = {month} AND code IS NOT NULL AND datetime IS NOT NULL "
        f"AND formatDateTime(datetime, '%H:%i') IN ({VALID_5M_TIMES}) {cutoff_clause} "
        "GROUP BY toDate(datetime), code ORDER BY toDate(datetime), code "
        "SETTINGS max_threads=1, max_memory_usage=1000000000"
    ).result_rows
    path = daily_count_file(run_id, month)
    digest = hashlib.sha256()
    anomalies = 0
    dates = set()
    with gzip.open(path, "wt", encoding="utf-8", newline="\n") as handle:
        for trade_date, code, count in rows:
            value = f"{trade_date}\t{code}\t{count}\n"
            handle.write(value)
            digest.update(value.encode("utf-8"))
            dates.add(str(trade_date))
            anomalies += int(count != 48)
    return {"daily_file": str(path), "daily_rows": len(rows), "daily_sha256": digest.hexdigest(),
            "daily_anomaly_rows": anomalies, "trade_dates": sorted(dates)}


def months_of_table(client, table: str) -> list[str]:
    rows = client.query(
        f"SELECT DISTINCT toYYYYMM(datetime) FROM {quote_identifier(table)} "
        "WHERE datetime IS NOT NULL ORDER BY 1"
    ).result_rows
    return [str(r[0]) for r in rows]


# ---------------------------------------------------------------- Phase 0

def cmd_baseline(args) -> int:
    """Freeze the 5m source: per-month rows/keys/sums/hash into an immutable manifest."""
    client = clickhouse_client()
    run_id = args.run_id or datetime.now().strftime("%Y%m%d_%H%M%S")
    cutoff = datetime.now().isoformat(timespec="seconds")
    months = months_of_table(client, SOURCE_TABLE)
    entries = []
    cutoff_clause = f" AND (created_at IS NULL OR created_at <= toDateTime('{cutoff}'))"
    for m in months:
        row = client.query(
            f"""
            SELECT
                count(),
                uniqExact(code, datetime),
                countIf(open IS NULL OR high IS NULL OR low IS NULL OR close IS NULL),
                countIf(volume < 0 OR amount < 0),
                sum(volume),
                sum(amount),
                sum({HASH_EXPR})
            FROM {SOURCE_TABLE} FINAL
            WHERE toYYYYMM(datetime) = {m}{cutoff_clause}
            SETTINGS max_threads=1, max_memory_usage=1500000000
            """
        ).result_rows[0]
        detail = write_daily_counts(client, run_id, m, cutoff_clause)
        entries.append(dict(
            month=m,
            **dict(zip(
                ("rows", "uniq_keys", "null_ohlc", "neg_values", "sum_volume", "sum_amount", "hash"),
                row,
            )),
            **detail,
        ))
    source_meta = table_schema(client, SOURCE_TABLE)
    source_watermark = client.query(
        f"SELECT max(created_at), max(datetime) FROM {SOURCE_TABLE} FINAL WHERE 1=1{cutoff_clause}"
    ).result_rows[0]
    manifest_source = {"schema": source_meta, "watermark": [str(x) for x in source_watermark]}
    try:
        stage = client.query(
            "SELECT count(), max(created_at), max(datetime), "
            f"sum({HASH_EXPR}) FROM qmt_xtquant_minute_stage WHERE period IN ('5','5m')"
        ).result_rows[0]
        manifest_source["qmt_stage_5m"] = [str(x) for x in stage]
    except Exception as exc:
        manifest_source["qmt_stage_5m_error"] = str(exc)
    fingerprint_src = json.dumps(entries, sort_keys=True, default=str)
    fingerprint = hashlib.sha256(fingerprint_src.encode("utf-8")).hexdigest()
    manifest = {
        "run_id": run_id,
        "cutoff": cutoff,
        "source_table": SOURCE_TABLE,
        "source_snapshot": manifest_source,
        "months": entries,
        "fingerprint": fingerprint,
    }
    path = manifest_path(run_id)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    tmp_path.replace(path)
    print(json.dumps({"run_id": run_id, "fingerprint": fingerprint, "months": len(months),
                      "manifest": str(path)}))
    return 0


def load_manifest(run_id: str) -> dict:
    safe_run_id(run_id)
    path = manifest_path(run_id)
    if not path.exists():
        raise SystemExit(f"baseline manifest not found: {path} (run `baseline` first)")
    return json.loads(path.read_text(encoding="utf-8"))


def require_complete_month(manifest: dict, month: str) -> dict:
    """Reject derived-table work when the frozen source has incomplete days.

    Aggregation uses ``HAVING count() = bars`` and therefore silently drops
    partial 5m buckets.  Publishing such a month would make a successful
    candidate/verification look complete while omitting source keys.  Keep
    baseline creation permissive for diagnosis, but make every build,
    verification, and promotion path fail closed.
    """
    month = safe_month(month)
    entries = [entry for entry in manifest.get("months", [])
               if safe_month(str(entry.get("month", ""))) == month]
    if len(entries) != 1:
        raise SystemExit(f"month {month} is not uniquely present in baseline manifest")
    entry = entries[0]
    anomalies = int(entry.get("daily_anomaly_rows", 0) or 0)
    if anomalies:
        raise SystemExit(
            f"baseline month {month} has {anomalies} incomplete code-days; "
            "repair/rebaseline source before derived migration"
        )
    return entry


def check_fingerprint(client, manifest: dict) -> None:
    """Re-derive the current 5m fingerprint; abort when the frozen source drifted."""
    cutoff_clause = source_cutoff_clause(manifest)
    for entry in manifest["months"]:
        m = safe_month(str(entry["month"]))
        row = client.query(
            f"""
            SELECT count(), uniqExact(code, datetime), countIf(open IS NULL OR high IS NULL OR low IS NULL OR close IS NULL),
                   countIf(volume < 0 OR amount < 0), sum(volume), sum(amount), sum({HASH_EXPR})
            FROM {SOURCE_TABLE} FINAL
            WHERE toYYYYMM(datetime) = {m}{cutoff_clause}
            SETTINGS max_threads = 1, max_memory_usage = 1500000000,
                     max_bytes_before_external_group_by = 500000000
            """
        ).result_rows[0]
        if entry.get("null_ohlc", 0) or entry.get("neg_values", 0) or entry["rows"] != entry["uniq_keys"]:
            raise SystemExit(f"5m baseline contains invalid or duplicate rows for month {m}")
        expected = [entry["rows"], entry["uniq_keys"], entry.get("null_ohlc", 0),
                    entry.get("neg_values", 0), entry.get("sum_volume"), entry.get("sum_amount"), entry["hash"]]
        if list(row) != expected:
            raise SystemExit(
                f"5m baseline drift detected for month {m}: "
                f"frozen={expected} current={list(row)} - candidate must be discarded and rebuilt"
            )
        detail_file = entry.get("daily_file")
        if detail_file and not Path(detail_file).exists():
            raise SystemExit(f"baseline daily detail missing for month {m}: {detail_file}")
        if detail_file and not entry.get("daily_sha256"):
            raise SystemExit(f"baseline detail digest missing for month {m}")
        # The manifest month set is frozen; newly appearing source months must not be invisible.
    current_months = months_of_table(client, SOURCE_TABLE)
    frozen_months = sorted(safe_month(str(e["month"])) for e in manifest["months"])
    if current_months != frozen_months:
        raise SystemExit(f"5m month set changed since baseline: frozen={frozen_months} current={current_months}")


# ---------------------------------------------------------------- Phase 2

def ensure_v2(client, period: str) -> str:
    table = f"kline_minute_{period}_v2"
    existing = table_schema(client, table)
    if existing:
        engine, partition_key, sorting_key = existing[0]
        if engine != "ReplacingMergeTree" or partition_key != "toYYYYMM(datetime)" or sorting_key not in ("code, datetime", "(code, datetime)"):
            raise SystemExit(
                f"existing {table} is not a monthly-partitioned migration table: {existing}; "
                "quarantine it and create a fresh candidate"
            )
        return table
    client.command(V2_DDL.replace("CREATE TABLE {table}", "CREATE TABLE {table}").format(table=table))
    return table


def aggregate_sql(period: str, month: str, cutoff_clause: str = "") -> str:
    """Session-aware 5m -> period aggregation, identical semantics to the
    approved rebuild_minute_60_from_5m.py (generalized per period)."""
    minutes = PERIOD_MINUTES[period]
    month = safe_month(month)
    bars = minutes // 5
    return f"""
    INSERT INTO {{target}} ({V2_COLUMNS})
    SELECT
        code,
        bucket,
        argMin(open, datetime),
        max(high),
        min(low),
        argMax(close, datetime),
        sum(volume),
        sum(amount),
        now(),
        cityHash64(code, toString(bucket), '{minutes}m')
    FROM
    (
        SELECT
            code, datetime, open, high, low, close, volume, amount,
            session_start + toIntervalMinute(
                if(elapsed_minutes <= 0, {minutes}, intDiv(elapsed_minutes + {minutes - 1}, {minutes}) * {minutes})
            ) AS bucket
        FROM
        (
            SELECT
                code, datetime, open, high, low, close, volume, amount,
                if(
                    toHour(datetime) < 12,
                    toStartOfDay(datetime) + toIntervalHour(9) + toIntervalMinute(30),
                    toStartOfDay(datetime) + toIntervalHour(13)
                ) AS session_start,
                dateDiff('minute', session_start, datetime) AS elapsed_minutes
            FROM {SOURCE_TABLE} FINAL
            WHERE toYYYYMM(datetime) = {month}{cutoff_clause}
              AND code IS NOT NULL AND datetime IS NOT NULL
              AND formatDateTime(datetime, '%H:%i') IN ({VALID_5M_TIMES})
        )
    )
    GROUP BY code, bucket
    HAVING count() = {bars}
    SETTINGS
        max_threads = 2,
        max_memory_usage = 1500000000,
        max_bytes_before_external_group_by = 500000000,
        optimize_aggregation_in_order = 1
    """


def cmd_create(args) -> int:
    client = clickhouse_client()
    table = ensure_v2(client, args.period)
    print(json.dumps({"created": table}))
    return 0


def cmd_build_month(args) -> int:
    client = clickhouse_client()
    manifest = load_manifest(args.run_id)
    check_fingerprint(client, manifest)
    month = safe_month(args.month)
    require_complete_month(manifest, month)
    table = f"kline_minute_{args.period}_v2"
    ensure_v2(client, args.period)

    if args.drop_existing_partition:
        client.command(f"ALTER TABLE {table} DROP PARTITION IF EXISTS {month}")

    existing = client.query(
        f"SELECT count() FROM {table} WHERE toYYYYMM(datetime) = {month}"
    ).result_rows[0][0]
    if existing:
        print(json.dumps({"month": month, "skipped": "already populated", "rows": existing}))
        return 0

    if args.period == "60" or args.from_5m:
        sql = aggregate_sql(args.period, month, source_cutoff_clause(manifest)).format(target=table)
        client.command(sql)
        source = "5m-aggregate"
    else:
        client.command(
            f"""
            INSERT INTO {table} ({V2_COLUMNS})
            SELECT {V2_COLUMNS} FROM kline_minute_{args.period} FINAL
            WHERE toYYYYMM(datetime) = {month}
            SETTINGS max_threads = 2, max_memory_usage = 1500000000
            """
        )
        source = "old-copy"
    rows = client.query(
        f"SELECT count() FROM {table} FINAL WHERE toYYYYMM(datetime) = {month}"
    ).result_rows[0][0]
    print(json.dumps({"month": month, "period": args.period, "source": source, "rows": rows}))
    return 0


# ---------------------------------------------------------------- verify

def cmd_verify_month(args) -> int:
    client = clickhouse_client()
    manifest = load_manifest(args.run_id)
    check_fingerprint(client, manifest)
    month = safe_month(args.month)
    require_complete_month(manifest, month)
    table = f"kline_minute_{args.period}_v2"

    if args.from_5m or args.period == "60":
        ref = _ref_alias(args.period, month, source_cutoff_clause(manifest))
        v2_missing = client.query(
            f"""
            SELECT count() FROM
              (SELECT code, datetime AS bucket FROM {table} FINAL WHERE toYYYYMM(datetime) = {month}) v
            ANTI LEFT JOIN ({ref}) r USING (code, bucket)
            """
        ).result_rows[0][0]
        ref_missing = client.query(
            f"""
            SELECT count() FROM ({ref}) r ANTI LEFT JOIN
              (SELECT code, datetime AS bucket FROM {table} FINAL WHERE toYYYYMM(datetime) = {month}) v
            USING (code, bucket)
            """
        ).result_rows[0][0]
        mismatch = client.query(
            f"""
            SELECT count() FROM
              (SELECT code, datetime AS bucket, open, high, low, close, volume, amount
               FROM {table} FINAL WHERE toYYYYMM(datetime) = {month}) v
            INNER JOIN ({ref}) r USING (code, bucket)
            WHERE abs(v.open - r.open) > 1e-6 OR abs(v.high - r.high) > 1e-6
               OR abs(v.low - r.low) > 1e-6 OR abs(v.close - r.close) > 1e-6
               OR abs(v.volume - r.volume) > 1e-3 OR abs(v.amount - r.amount) > 0.01
            SETTINGS max_threads = 2, max_memory_usage = 1500000000
            """
        ).result_rows[0][0]
        ok = v2_missing == 0 and ref_missing == 0 and mismatch == 0
        print(json.dumps({
            "month": month, "period": args.period, "reference": "5m-aggregate",
            "v2_keys_not_in_ref": v2_missing, "ref_keys_not_in_v2": ref_missing,
            "value_mismatches": mismatch, "match": ok,
        }))
        return 0 if ok else 1

    row = client.query(
        f"""
        SELECT
            (SELECT count() FROM {table} FINAL WHERE toYYYYMM(datetime) = {month}),
            (SELECT count() FROM kline_minute_{args.period} FINAL WHERE toYYYYMM(datetime) = {month}),
            (SELECT uniqExact(code, datetime) FROM {table} FINAL WHERE toYYYYMM(datetime) = {month}),
            (SELECT uniqExact(code, datetime) FROM kline_minute_{args.period} FINAL WHERE toYYYYMM(datetime) = {month})
        """
    ).result_rows[0]
    ok = row[0] == row[1] and row[2] == row[3] and row[0] == row[2]
    print(json.dumps({
        "month": month, "period": args.period, "reference": "old-table",
        "v2_rows": row[0], "old_rows": row[1], "v2_uniq": row[2], "old_uniq": row[3],
        "match": ok,
    }))
    return 0 if ok else 1


def _ref_alias(period: str, month: str, cutoff_clause: str = "") -> str:
    """Reusable 5m-aggregation reference subquery (per-bucket OHLCV rows)."""
    minutes = PERIOD_MINUTES[period]
    month = safe_month(month)
    bars = minutes // 5
    return f"""
        SELECT code, bucket,
               argMin(open, datetime) AS open, max(high) AS high, min(low) AS low,
               argMax(close, datetime) AS close, sum(volume) AS volume, sum(amount) AS amount
        FROM
        (
            SELECT code, datetime, open, high, low, close, volume, amount,
                session_start + toIntervalMinute(
                    if(elapsed_minutes <= 0, {minutes}, intDiv(elapsed_minutes + {minutes - 1}, {minutes}) * {minutes})
                ) AS bucket
            FROM
            (
                SELECT code, datetime, open, high, low, close, volume, amount,
                    if(toHour(datetime) < 12,
                       toStartOfDay(datetime) + toIntervalHour(9) + toIntervalMinute(30),
                       toStartOfDay(datetime) + toIntervalHour(13)) AS session_start,
                    dateDiff('minute', session_start, datetime) AS elapsed_minutes
                FROM {SOURCE_TABLE} FINAL
                WHERE toYYYYMM(datetime) = {month}{cutoff_clause}
                  AND code IS NOT NULL AND datetime IS NOT NULL
                  AND formatDateTime(datetime, '%H:%i') IN ({VALID_5M_TIMES})
            )
        )
        GROUP BY code, bucket
        HAVING count() = {bars}
        SETTINGS max_threads = 2, max_memory_usage = 1500000000,
                 max_bytes_before_external_group_by = 500000000
    """


def aggregate_reference_sql(minutes: int, bars: int, month: str, cutoff_clause: str = "") -> str:
    month = safe_month(month)
    derived_hash = "cityHash64(ifNull(code,''), toString(bucket), toString(open), toString(high), toString(low), toString(close), toString(volume), toString(amount))"
    return f"""
    SELECT count(), uniqExact(code, bucket), sum({derived_hash}) FROM (
        SELECT
            code,
            bucket,
            argMin(open, datetime) AS open,
            max(high) AS high,
            min(low) AS low,
            argMax(close, datetime) AS close,
            sum(volume) AS volume,
            sum(amount) AS amount
        FROM
        (
            SELECT code, datetime, open, high, low, close, volume, amount,
                session_start + toIntervalMinute(
                    if(elapsed_minutes <= 0, {minutes}, intDiv(elapsed_minutes + {minutes - 1}, {minutes}) * {minutes})
                ) AS bucket
            FROM
            (
                SELECT code, datetime, open, high, low, close, volume, amount,
                    if(toHour(datetime) < 12,
                       toStartOfDay(datetime) + toIntervalHour(9) + toIntervalMinute(30),
                       toStartOfDay(datetime) + toIntervalHour(13)) AS session_start,
                    dateDiff('minute', session_start, datetime) AS elapsed_minutes
                FROM {SOURCE_TABLE} FINAL
                WHERE toYYYYMM(datetime) = {month}{cutoff_clause}
                  AND code IS NOT NULL AND datetime IS NOT NULL
                  AND formatDateTime(datetime, '%H:%i') IN ({VALID_5M_TIMES})
            )
        )
        GROUP BY code, bucket
        HAVING count() = {bars}
    )
    SETTINGS max_threads = 2, max_memory_usage = 1500000000,
             max_bytes_before_external_group_by = 500000000
    """


# ---------------------------------------------------------------- Phase 3

def cmd_exchange(args) -> int:
    require_freeze()
    client = clickhouse_client()
    manifest = load_manifest(args.run_id)
    check_fingerprint(client, manifest)
    period = args.period
    official = f"kline_minute_{period}"
    v2 = f"kline_minute_{period}_v2"

    months = months_of_table(client, v2)
    source_months = months_of_table(client, SOURCE_TABLE)
    if months != source_months:
        raise SystemExit(f"v2/source month set mismatch: v2={months} source={source_months}")
    manifest_months = sorted(safe_month(str(e["month"])) for e in manifest["months"])
    if months != manifest_months:
        raise SystemExit(f"v2/manifest month set mismatch: v2={months} manifest={manifest_months}")
    ddl = client.query(
        "SELECT partition_key, sorting_key, engine FROM system.tables "
        f"WHERE database='stock' AND name='{official}'"
    ).result_rows
    if not ddl or ddl[0][0] or ddl[0][1] not in ('code, datetime', '(code, datetime)'):
        if ddl and ddl[0][0] == 'toYYYYMM(datetime)':
            raise SystemExit(f"{official} is already monthly-partitioned; exchange is already complete")
        raise SystemExit(f"unexpected official table schema before exchange: {ddl}")
    v2_ddl = client.query(
        "SELECT partition_key, sorting_key, engine FROM system.tables "
        f"WHERE database='stock' AND name='{v2}'"
    ).result_rows
    if not v2_ddl or v2_ddl[0][0] != 'toYYYYMM(datetime)' or v2_ddl[0][1] != 'code, datetime':
        raise SystemExit(f"unexpected v2 table schema: {v2_ddl}")
    for m in months:
        row = client.query(
            f"SELECT count(), uniqExact(code, datetime) FROM {v2} FINAL WHERE toYYYYMM(datetime) = {m}"
        ).result_rows[0]
        if not row[0] or row[0] != row[1]:
            raise SystemExit(f"v2 month {m} failed completeness: rows/uniq={row}")

    if not args.yes:
        raise SystemExit("pass --yes to perform the atomic EXCHANGE TABLES")

    client.command(f"EXCHANGE TABLES {official} AND {v2}")
    after = client.query(
        f"SELECT count(), min(datetime), max(datetime) FROM {official}"
    ).result_rows[0]
    print(json.dumps({
        "exchanged": official, "old_table_now": v2,
        "official_after": dict(zip(("rows", "min_dt", "max_dt"), [str(x) for x in after])),
        "rollback": f"EXCHANGE TABLES {official} AND {v2}",
    }))
    del source_months
    return 0


# ---------------------------------------------------------------- Phase 4

def cmd_candidate(args) -> int:
    """Build a month-scoped candidate table from the frozen 5m baseline."""
    safe_run_id(args.run_id)
    client = clickhouse_client()
    manifest = load_manifest(args.run_id)
    check_fingerprint(client, manifest)
    month = safe_month(args.month)
    require_complete_month(manifest, month)
    candidate = f"kline_minute_{args.period}_candidate_{month}"
    exists = client.query(
        "SELECT count() FROM system.tables WHERE database='stock' AND name=%(name)s",
        parameters={"name": candidate},
    ).result_rows[0][0]
    if exists and not args.drop_existing:
        raise SystemExit(f"candidate already exists: {candidate}; pass --drop-existing to rebuild it")
    if exists:
        client.command(f"DROP TABLE {candidate}")
    client.command(V2_DDL.format(table=candidate))
    client.command(aggregate_sql(args.period, month, source_cutoff_clause(manifest)).format(target=candidate))
    schema = table_schema(client, candidate)
    if not schema or schema[0][1] != "toYYYYMM(datetime)" or schema[0][2] not in ("code, datetime", "(code, datetime)"):
        raise SystemExit(f"candidate schema gate failed: {schema}")
    row = client.query(
        f"SELECT count(), uniqExact(code, datetime), sum({HASH_EXPR}) FROM {candidate} FINAL"
    ).result_rows[0]
    if not row[0] or row[0] != row[1]:
        write_state("candidate", args.period, month, args.run_id, status="stale_source",
                    table=candidate, reason="empty_or_duplicate_keys")
        raise SystemExit(f"candidate uniqueness gate failed: {row}")
    # Validate the candidate against an independent frozen-source aggregate
    # before declaring it ready.  replace-partition repeats this gate, but a
    # candidate should never look verified merely because its keys are unique.
    expected = client.query(aggregate_reference_sql(
        int(args.period), int(args.period) // 5, month,
        source_cutoff_clause(manifest),
    )).result_rows[0]
    if list(expected) != list(row):
        write_state("candidate", args.period, month, args.run_id, status="stale_source",
                    table=candidate, reason="reference_mismatch", expected=expected, actual=row)
        raise SystemExit(f"candidate/reference mismatch: expected={expected} actual={row}")
    write_state("candidate", args.period, month, args.run_id, status="built_verified", table=candidate,
                rows=row[0], uniq_keys=row[1], hash=str(row[2]))
    print(json.dumps({"candidate": candidate, "rows": row[0], "uniq_keys": row[1], "hash": str(row[2]),
                      "reference_match": True}))
    return 0


def cmd_replace_partition(args) -> int:
    require_freeze()
    safe_run_id(args.run_id)
    client = clickhouse_client()
    manifest = load_manifest(args.run_id)
    check_fingerprint(client, manifest)
    month = safe_month(args.month)
    require_complete_month(manifest, month)
    period = args.period
    official = f"kline_minute_{period}"
    candidate = f"kline_minute_{period}_candidate_{month}"
    rollback = f"kline_minute_{period}_rollback_{month}_{args.run_id}"

    exists = client.query(
        "SELECT count() FROM system.tables WHERE database='stock' AND name=%(name)s",
        parameters={"name": candidate},
    ).result_rows[0][0]
    if not exists:
        raise SystemExit(f"candidate table does not exist: {candidate}")
    candidate_months = months_of_table(client, candidate)
    if candidate_months != [month]:
        raise SystemExit(f"candidate must contain exactly month {month}, got {candidate_months}")
    candidate_schema = table_schema(client, candidate)
    official_schema = table_schema(client, official)
    if not candidate_schema or not official_schema or candidate_schema[0][1:] != official_schema[0][1:]:
        raise SystemExit(f"candidate/official schema mismatch: candidate={candidate_schema} official={official_schema}")
    row = client.query(
        f"SELECT count(), uniqExact(code, datetime) FROM {candidate} FINAL"
    ).result_rows[0]
    if not row[0] or row[0] != row[1]:
        raise SystemExit(f"candidate failed uniqueness gate: {row}")
    ref = aggregate_reference_sql(int(args.period), int(args.period) // 5, month,
                                  source_cutoff_clause(manifest))
    expected = client.query(ref).result_rows[0]
    actual = client.query(f"SELECT count(), uniqExact(code, datetime), sum({HASH_EXPR}) FROM {candidate} FINAL").result_rows[0]
    if list(expected) != list(actual):
        write_state("candidate", args.period, month, args.run_id, status="stale_source",
                    table=candidate, reason="reference_mismatch", expected=expected, actual=actual)
        raise SystemExit(f"candidate/reference mismatch: expected={expected} actual={actual}")
    if not args.yes:
        raise SystemExit("pass --yes to REPLACE PARTITION on the official table")

    # Keep a physical rollback copy of the old partition before mutating the official table.
    client.command(f"DROP TABLE IF EXISTS {rollback}")
    client.command(f"CREATE TABLE {rollback} AS {official}")
    client.command(f"ALTER TABLE {rollback} REPLACE PARTITION {month} FROM {official}")
    try:
        check_fingerprint(client, manifest)
        client.command(f"ALTER TABLE {official} REPLACE PARTITION {month} FROM {candidate}")
        after = client.query(
            f"SELECT count(), uniqExact(code, datetime), sum({HASH_EXPR}) "
            f"FROM {official} FINAL WHERE toYYYYMM(datetime) = {month}"
        ).result_rows[0]
        expected = client.query(
            f"SELECT count(), uniqExact(code, datetime), sum({HASH_EXPR}) "
            f"FROM {candidate} FINAL"
        ).result_rows[0]
        if list(after) != list(expected):
            raise RuntimeError(f"post-replace candidate mismatch: official={after} candidate={expected}")
    except Exception:
        # Restore only the affected partition; never leave a partially published month.
        client.command(f"ALTER TABLE {official} REPLACE PARTITION {month} FROM {rollback}")
        write_state("replace", period, month, args.run_id, status="rolled_back", rollback=rollback)
        raise
    write_state("replace", period, month, args.run_id, status="published", rollback=rollback,
                rows=after[0], uniq_keys=after[1], hash=str(after[2]))
    print(json.dumps({"replaced": official, "month": month, "rows": after[0],
                      "uniq_keys": after[1], "rollback": rollback}))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("baseline")
    p.add_argument("--run-id")
    p.set_defaults(fn=cmd_baseline)

    p = sub.add_parser("create")
    p.add_argument("--period", required=True, choices=PERIOD_MINUTES)
    p.set_defaults(fn=cmd_create)

    p = sub.add_parser("build-month")
    p.add_argument("--period", required=True, choices=PERIOD_MINUTES)
    p.add_argument("--month", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--from-5m", action="store_true",
                   help="aggregate from 5m instead of copying old table (forced for 60m)")
    p.add_argument("--drop-existing-partition", action="store_true")
    p.set_defaults(fn=cmd_build_month)

    p = sub.add_parser("verify-month")
    p.add_argument("--period", required=True, choices=PERIOD_MINUTES)
    p.add_argument("--month", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--from-5m", action="store_true")
    p.set_defaults(fn=cmd_verify_month)

    p = sub.add_parser("exchange")
    p.add_argument("--period", required=True, choices=PERIOD_MINUTES)
    p.add_argument("--run-id", required=True)
    p.add_argument("--yes", action="store_true")
    p.set_defaults(fn=cmd_exchange)

    p = sub.add_parser("candidate")
    p.add_argument("--period", required=True, choices=PERIOD_MINUTES)
    p.add_argument("--month", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--drop-existing", action="store_true")
    p.set_defaults(fn=cmd_candidate)

    p = sub.add_parser("replace-partition")
    p.add_argument("--period", required=True, choices=PERIOD_MINUTES)
    p.add_argument("--month", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--yes", action="store_true")
    p.set_defaults(fn=cmd_replace_partition)

    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())

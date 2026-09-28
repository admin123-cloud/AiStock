"""Promote only current A-grade purchase-source bars and clear verified false review items.

The default run is read-only and produces a plan. --apply revalidates every
candidate, snapshots exact formal rows, then applies scoped monthly mutations.
B/C source candidates remain manual. 399008.SZ rows before index publication
and verified zero-turnover suspension bars are excluded from formal backtest
tables and from the next active review projection; original source/audit data
is retained.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from clickhouse_connect.driver.external import ExternalData

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_client
from utils.minute_daily_quality_gate import EXPECTED_UNITS, evaluate_code_day
from utils.paths import runtime_path


BASE_RUN = "purchased_history_review_20260922_refit_a_b_confirmed_20260922_v13_harry_a2_2pct"
NEW_RUN = "purchased_history_review_20260923_envelope_a_lifecycle_cleared_v1"
GATE_RUN = "purchased_history_review_20260923_purchase_daily_envelope_audit_v3"
INDEX_CODE = "399008.SZ"
INDEX_START = date(2010, 3, 22)
PURCHASED_FAMILY = "purchased_a_share_history"
ACTIVE_MANIFEST = runtime_path("purchased_history", "data_review_manifest.json")
AUDIT_ROOT = runtime_path("quality_plan", "pre2010_purchase_daily_envelope_20260923_v3")
BLOCKER_ROOT = runtime_path("quality_plan", "pre2010_gate_blockers_20260923_v1")
OUT = runtime_path("quality_plan", "pre2010_envelope_a_closeout_20260923_v1")
A_CSV = AUDIT_ROOT / "a_grade_ready_for_promotion.csv"
NO_TRADE_CSV = BLOCKER_ROOT / "baostock_confirmed_suspension_zero_minute_candidates.csv"
NO_EXACT_CSV = BLOCKER_ROOT / "baostock_no_exact_daily_row.csv"
QUEUE = "stock.purchased_history_data_review_items"
DAILY_COMPARE = "stock.purchased_daily_user_policy_compare_2000_2010"
EXPECTED = {"15m": 16, "30m": 8, "60m": 4}
PERIOD_TABLE = {
    "5m": "stock.kline_minute_5",
    "15m": "stock.kline_minute_15",
    "30m": "stock.kline_minute_30",
    "60m": "stock.kline_minute_60",
    "1d": "stock.kline_daily",
}
PERIOD_CANDIDATE = {
    "15m": "stock.purchased_native_15m_candidate_user_policy_20260921",
    "30m": "stock.purchased_native_30m_candidate_user_policy_20260921",
    "60m": "stock.purchased_native_60m_candidate_user_policy_20260921",
}
STAGE = {p: f"stock.pre2010_envelope_a_{p}_candidate_20260923_v1" for p in EXPECTED}
BACKUP = {
    p: f"stock.{table.split('.')[1]}_repair_backup_envelope_closeout_20260923_v1"
    for p, table in PERIOD_TABLE.items()
}
DELETE_KEYS = {p: f"stock.pre2010_envelope_delete_keys_{p}_20260923_v1" for p in PERIOD_TABLE}
NO_TRADE_TABLE = "stock.pre2010_no_trade_resolution_20260923_v1"
LIFECYCLE_AUDIT = "stock.pre2010_lifecycle_exclusion_audit_20260923_v1"
SH_TZ = ZoneInfo("Asia/Shanghai")
MINUTE_COLUMNS = ["code", "datetime", "open", "high", "low", "close", "volume", "amount", "created_at", "id"]
QUEUE_COLUMNS = [
    "run_id", "review_key", "issue_type", "source", "code", "trade_date", "period",
    "security_type", "expected_count", "actual_count", "missing_count", "source_batch_id",
    "source_archive", "source_entry", "evidence_ref", "evidence_sha256", "observed_at", "reason",
]


def _load_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _external(name: str, headers: list[str], types: list[str], rows: list[list[Any]]) -> ExternalData:
    stream = io.StringIO()
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(headers)
    writer.writerows(rows)
    return ExternalData(
        data=stream.getvalue().encode("utf-8"),
        file_name=f"{name}.csv",
        fmt="CSVWithNames",
        structure=[f"{name} {kind}" for name, kind in zip(headers, types)],
    )


def _safe_code(value: str) -> str:
    code = str(value).strip().upper()
    if not re.fullmatch(r"[0-9]{6}\.(SH|SZ|BJ)", code):
        raise ValueError(f"invalid canonical code: {value!r}")
    return code


def _merge_days(days: list[date]) -> list[tuple[date, date]]:
    ordered = sorted(set(days))
    if not ordered:
        return []
    result: list[tuple[date, date]] = []
    start = prev = ordered[0]
    for current in ordered[1:]:
        if current == prev + timedelta(days=1):
            prev = current
            continue
        result.append((start, prev + timedelta(days=1)))
        start = prev = current
    result.append((start, prev + timedelta(days=1)))
    return result


def _datetime_filter(column: str, intervals: list[tuple[date, date]]) -> str:
    if not intervals:
        return "0"
    return " OR ".join(
        f"({column} >= toDateTime('{start.isoformat()} 00:00:00') AND {column} < toDateTime('{end.isoformat()} 00:00:00'))"
        for start, end in intervals
    )


def _date_filter(column: str, intervals: list[tuple[date, date]]) -> str:
    if not intervals:
        return "0"
    return " OR ".join(
        f"({column} >= toDate('{start.isoformat()}') AND {column} < toDate('{end.isoformat()}'))"
        for start, end in intervals
    )


def _candidate_sql(period: str) -> str:
    table = PERIOD_CANDIDATE[period]
    date_join = "p.trade_date=k.trade_date" if period == "15m" else "toDate(p.datetime)=k.trade_date"
    source_join = ""
    if period == "15m":
        source_join = f"""
        INNER JOIN (
            SELECT q.code,q.trade_date,any(a.chosen_source) AS chosen_source,
                   any(a.batch_id) AS chosen_batch
            FROM {QUEUE} q
            LEFT JOIN stock.purchased_native_15m_candidate_user_policy_20260921_day_audit a
                USING(code,trade_date)
            WHERE q.run_id='{BASE_RUN}' AND q.period='15m'
            GROUP BY q.code,q.trade_date
        ) chosen ON p.code=chosen.code AND p.trade_date=chosen.trade_date
                 AND p.source_kind=chosen.chosen_source AND p.batch_id=chosen.chosen_batch
        """
    source_ref = "p.provenance" if period == "15m" else "p.source_ref"
    units = "p.volume_unit,p.amount_unit" if period == "15m" else "'lots' AS volume_unit,'yuan' AS amount_unit"
    return f"""
        SELECT p.code, toDate(p.datetime) AS trade_date, p.datetime,
               p.open,p.high,p.low,p.close,p.volume,p.amount,
               p.source_kind,p.quality_status,{units},{source_ref} AS source_ref,
               now() AS created_at,
               cityHash64(concat(p.code,'|',toString(p.datetime),'|{NEW_RUN}')) AS id,
               d.open AS daily_open,d.high AS daily_high,d.low AS daily_low,
               d.close AS daily_close,d.volume AS daily_volume,d.amount AS daily_amount
        FROM {table} p
        INNER JOIN a_keys k ON p.code=k.code AND {date_join} AND k.period='{period}'
        {source_join}
        INNER JOIN {DAILY_COMPARE} d ON d.code=p.code AND d.trade_date=toDate(p.datetime)
        WHERE p.source_year BETWEEN 2000 AND 2010
    """


def _read_candidate_rows(client, keys: list[dict[str, str]]) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    ext_rows = [[r["code"], r["trade_date"], r["period"]] for r in keys]
    ext = _external("a_keys", ["code", "trade_date", "period"], ["String", "Date", "String"], ext_rows)
    grouped: dict[str, dict[tuple[str, date], list[dict[str, Any]]]] = {p: defaultdict(list) for p in EXPECTED}
    formal_rows: dict[str, list[dict[str, Any]]] = {p: [] for p in EXPECTED}
    cols = ["code", "trade_date", "datetime", "open", "high", "low", "close", "volume", "amount",
            "source_kind", "quality_status", "volume_unit", "amount_unit", "source_ref", "created_at", "id",
            "daily_open", "daily_high", "daily_low", "daily_close", "daily_volume", "daily_amount"]
    for period in EXPECTED:
        result = client.query(_candidate_sql(period), external_data=ext,
                              settings={"max_execution_time": 600, "max_threads": 2})
        for values in result.result_rows:
            rec = dict(zip(cols, values))
            rec["code"] = str(rec["code"])
            rec["trade_date"] = rec["trade_date"]
            key = (rec["code"], rec["trade_date"])
            grouped[period][key].append(rec)
            formal_rows[period].append(rec)
    checks: list[dict[str, Any]] = []
    expected_keys = {(r["period"], r["code"], date.fromisoformat(r["trade_date"])) for r in keys}
    actual_keys: set[tuple[str, str, date]] = set()
    allow_sources = {
        "15m": {"native_purchased_15m", "aggregate_5m"},
        "30m": {"native_purchased", "five_minute_derived"},
        "60m": {"native_purchased"},
    }
    allow_quality = {
        "15m": {"native_complete", "five_complete_fallback"},
        "30m": {"native_complete_code_day", "five_minute_complete_code_day"},
        "60m": {"native_complete_code_day"},
    }
    for period, days in grouped.items():
        for (code, day), bars in days.items():
            actual_keys.add((period, code, day))
            sources = {str(row["source_kind"]) for row in bars}
            qualities = {str(row["quality_status"]) for row in bars}
            units = {(str(row["volume_unit"]), str(row["amount_unit"])) for row in bars}
            if len(sources) != 1 or not sources <= allow_sources[period]:
                raise RuntimeError(f"A source ambiguity: {period} {code} {day} {sources}")
            if not qualities or not qualities <= allow_quality[period]:
                raise RuntimeError(f"A quality status mismatch: {period} {code} {day} {qualities}")
            if period == "15m" and units != {("lots", "yuan")}:
                raise RuntimeError(f"A unit mismatch: {period} {code} {day} {units}")
            gate_bars = [{"datetime": r["datetime"], "open": r["open"], "high": r["high"],
                          "low": r["low"], "close": r["close"], "volume": r["volume"],
                          "amount": r["amount"], "price_adjustment": "none"} for r in bars]
            first = bars[0]
            daily = {"open": first["daily_open"], "high": first["daily_high"],
                     "low": first["daily_low"], "close": first["daily_close"],
                     "volume_lots": first["daily_volume"], "amount_yuan": first["daily_amount"]}
            gate = evaluate_code_day(
                gate_bars, trade_date=day, daily=daily, daily_rows=1,
                evidence_complete=True, source_units=EXPECTED_UNITS, price_adjustment="none",
                period=period, minute_source_family=PURCHASED_FAMILY,
                daily_source_family=PURCHASED_FAMILY,
            )
            if gate.status != "accepted":
                raise RuntimeError(f"A gate no longer passes: {period} {code} {day} {gate.reasons}")
            expected_a = next(r for r in keys if r["period"] == period and r["code"] == code and date.fromisoformat(r["trade_date"]) == day)
            if int(expected_a["minute_rows"]) != gate.minute_rows:
                raise RuntimeError(f"A source row count drift: {period} {code} {day}")
            for csv_field, current in (("minute_high", gate.aggregate.high), ("minute_low", gate.aggregate.low),
                                       ("minute_volume_lots", gate.aggregate.volume_lots),
                                       ("minute_amount_yuan", gate.aggregate.amount_yuan)):
                if not math.isclose(float(expected_a[csv_field]), float(current), rel_tol=1e-12, abs_tol=1e-6):
                    raise RuntimeError(f"A audit snapshot drift: {period} {code} {day} {csv_field}")
            checks.append({
                "period": period, "code": code, "trade_date": day.isoformat(),
                "source_kind": next(iter(sources)), "source_ref": str(first["source_ref"]),
                "bar_count": gate.minute_rows, "unique_bars": gate.unique_datetime_rows,
                "daily_high": float(first["daily_high"]), "minute_high": gate.aggregate.high,
                "daily_low": float(first["daily_low"]), "minute_low": gate.aggregate.low,
                "daily_volume_lots": float(first["daily_volume"]), "minute_volume_lots": gate.aggregate.volume_lots,
                "daily_amount_yuan": float(first["daily_amount"]), "minute_amount_yuan": gate.aggregate.amount_yuan,
                "auction_residual_volume_lots": gate.auction_residual_volume_lots,
                "auction_residual_amount_yuan": gate.auction_residual_amount_yuan,
                "gate": "A_pass",
            })
    if actual_keys != expected_keys:
        missing = sorted(expected_keys - actual_keys)[:10]
        extra = sorted(actual_keys - expected_keys)[:10]
        raise RuntimeError(f"A candidate key set drift: missing={missing} extra={extra}")
    return formal_rows, checks


def _scan_minute_rows_for_code(code: str, days: list[date]) -> list[tuple[Any, ...]]:
    if not days:
        return []
    if not re.fullmatch(r"[0-9]{6}\.(SH|SZ|BJ)", code):
        raise ValueError(f"invalid code in scan: {code}")
    intervals = _merge_days(days)
    predicate = _datetime_filter("datetime", intervals)
    queries = []
    for period in ("5m", "15m", "30m", "60m"):
        table = PERIOD_TABLE[period]
        queries.append(f"""
            SELECT '{period}' AS period, code, toDate(datetime) AS trade_date, datetime,
                   open,high,low,close,toFloat64(volume) AS volume,toFloat64(amount) AS amount,
                   created_at,id
            FROM {table}
            WHERE code='{code}' AND ({predicate})
        """)
    client = clickhouse_client()
    result = client.query(" UNION ALL ".join(queries), settings={"max_execution_time": 600, "max_threads": 1})
    return result.result_rows


def _scan_daily_rows_for_code(code: str, days: list[date]) -> list[tuple[Any, ...]]:
    if not days:
        return []
    intervals = _merge_days(days)
    predicate = _date_filter("trade_date", intervals)
    client = clickhouse_client()
    return client.query(f"""
        SELECT code,trade_date,open,high,low,close,volume,amount,amplitude,change_pct,
               change_amount,turnover_rate,created_at,id
        FROM stock.kline_daily WHERE code='{code}' AND ({predicate})
    """, settings={"max_execution_time": 600, "max_threads": 1}).result_rows


def _scan_preindex_rows(client) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    cutoff = INDEX_START.isoformat()
    queries = []
    for period in ("5m", "15m", "30m", "60m"):
        table = PERIOD_TABLE[period]
        queries.append(f"""
            SELECT '{period}' AS period,code,toDate(datetime) AS trade_date,datetime,
                   open,high,low,close,toFloat64(volume) AS volume,toFloat64(amount) AS amount,
                   created_at,id
            FROM {table}
            WHERE code='{INDEX_CODE}' AND datetime<toDateTime('{cutoff} 00:00:00')
        """)
    minute_rows = client.query(" UNION ALL ".join(queries), settings={"max_execution_time": 600, "max_threads": 2}).result_rows
    daily_rows = client.query(f"""
        SELECT code,trade_date,open,high,low,close,volume,amount,amplitude,change_pct,
               change_amount,turnover_rate,created_at,id
        FROM stock.kline_daily WHERE code='{INDEX_CODE}' AND trade_date<toDate('{cutoff}')
    """, settings={"max_execution_time": 600, "max_threads": 2}).result_rows
    return minute_rows, daily_rows


def _scan_review_queue(client) -> list[dict[str, Any]]:
    result = client.query(f"""
        SELECT {','.join(QUEUE_COLUMNS)} FROM {QUEUE} WHERE run_id='{BASE_RUN}'
        ORDER BY trade_date,code,period
    """, settings={"max_execution_time": 600, "max_threads": 4})
    if not result.result_rows:
        raise RuntimeError(f"active base queue is empty: {BASE_RUN}")
    return [dict(zip(QUEUE_COLUMNS, row)) for row in result.result_rows]


def _table_exists(client, table: str) -> bool:
    db, name = table.split(".", 1)
    return bool(client.query(
        "SELECT count() FROM system.tables WHERE database=%(db)s AND name=%(name)s",
        parameters={"db": db, "name": name},
    ).result_rows[0][0])


def _check_audit_and_manifest(client) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]], list[dict[str, Any]]]:
    manifest = json.loads(ACTIVE_MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("run_id") != BASE_RUN:
        raise RuntimeError(f"active manifest moved: expected {BASE_RUN}, got {manifest.get('run_id')}")
    a_rows = _load_csv(A_CSV)
    nt_rows = _load_csv(NO_TRADE_CSV)
    no_exact_rows = _load_csv(NO_EXACT_CSV)
    if len(a_rows) != 1029:
        raise RuntimeError(f"unexpected A candidate count: {len(a_rows)}")
    a_keys = [( _safe_code(r["code"]), r["trade_date"], r["period"]) for r in a_rows]
    if len(set(a_keys)) != len(a_keys):
        raise RuntimeError("duplicate A candidate keys")
    period_counts = Counter(r["period"] for r in a_rows)
    expected_counts = {"15m": 456, "30m": 90, "60m": 483}
    if dict(period_counts) != expected_counts:
        raise RuntimeError(f"A period distribution drift: {dict(period_counts)}")
    nt_map: dict[tuple[str, date], dict[str, str]] = {}
    for row in nt_rows:
        code, day = _safe_code(row["code"]), date.fromisoformat(row["trade_date"])
        if row.get("external_status") != "confirmed_suspended_no_trade" or row.get("any_minute_positive", "").casefold() != "false":
            raise RuntimeError(f"invalid no-trade source candidate: {code} {day}")
        nt_map[(code, day)] = row
    if len(nt_map) != 5711:
        raise RuntimeError(f"unexpected zero-minute suspension candidate count: {len(nt_map)}")
    if any(_safe_code(r["code"]) != INDEX_CODE or date.fromisoformat(r["trade_date"]) >= INDEX_START for r in no_exact_rows):
        raise RuntimeError("no-exact-day evidence no longer matches pre-publication index scope")
    queue = _scan_review_queue(client)
    if len(queue) != 70220:
        raise RuntimeError(f"base queue row count drift: {len(queue)}")
    if len({(r["code"], r["trade_date"], r["period"]) for r in queue}) != len(queue):
        raise RuntimeError("duplicate code-date-period row in active queue")
    queue_keys = {(r["code"], r["trade_date"], r["period"]) for r in queue}
    missing_a = set((code, date.fromisoformat(day), period) for code, day, period in a_keys) - queue_keys
    if missing_a:
        raise RuntimeError(f"A keys are not all present in current review queue: {len(missing_a)}")
    return a_rows, nt_rows, no_exact_rows, queue


def _formal_target_rows(client, a_rows: list[dict[str, str]], nt_rows: list[dict[str, str]]) -> tuple[
    dict[str, list[tuple[Any, ...]]], list[tuple[Any, ...]], set[tuple[str, date]], dict[str, Any]
]:
    a_keys = {(r["period"], r["code"], date.fromisoformat(r["trade_date"])) for r in a_rows}
    no_trade = {(r["code"], date.fromisoformat(r["trade_date"])) for r in nt_rows}
    code_days: dict[str, set[date]] = defaultdict(set)
    for _, code, day in a_keys:
        code_days[code].add(day)
    for code, day in no_trade:
        code_days[code].add(day)
    scan_rows: list[tuple[Any, ...]] = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        future_map = {pool.submit(_scan_minute_rows_for_code, code, sorted(days)): code for code, days in code_days.items()}
        completed = 0
        for future in as_completed(future_map):
            scan_rows.extend(future.result())
            completed += 1
            if completed % 150 == 0:
                print(json.dumps({"formal_probe_codes_done": completed, "total_codes": len(future_map)}), flush=True)
    daily_rows: list[tuple[Any, ...]] = []
    daily_days: dict[str, set[date]] = defaultdict(set)
    for code, day in no_trade:
        daily_days[code].add(day)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_scan_daily_rows_for_code, code, sorted(days)) for code, days in daily_days.items()]
        for future in as_completed(futures):
            daily_rows.extend(future.result())
    preindex_minute, preindex_daily = _scan_preindex_rows(client)
    scan_rows.extend(preindex_minute)
    daily_rows.extend(preindex_daily)

    formal_rows: dict[str, list[tuple[Any, ...]]] = {p: [] for p in PERIOD_TABLE}
    for row in scan_rows:
        period, code, day = str(row[0]), str(row[1]), row[2]
        if period == "1d":
            continue
        if period == "5m" and ((code, day) in no_trade or (code == INDEX_CODE and day < INDEX_START)):
            formal_rows[period].append(row)
        elif period != "5m" and ((code, day) in no_trade or (code == INDEX_CODE and day < INDEX_START)
                                  or (period, code, day) in a_keys):
            formal_rows[period].append(row)

    daily_targets = [r for r in daily_rows if (str(r[0]), r[1]) in no_trade or (str(r[0]) == INDEX_CODE and r[1] < INDEX_START)]
    no_trade_stats: dict[tuple[str, date], dict[str, Any]] = {key: {"rows": 0, "positive_or_invalid": 0, "minute_5m_rows": 0} for key in no_trade}
    for row in scan_rows:
        period, code, day = str(row[0]), str(row[1]), row[2]
        if (code, day) not in no_trade:
            continue
        status = no_trade_stats[(code, day)]
        status["rows"] += 1
        if period == "5m":
            status["minute_5m_rows"] += 1
        volume, amount = row[8], row[9]
        if volume is None or amount is None or float(volume) != 0 or float(amount) != 0:
            status["positive_or_invalid"] += 1
    for row in daily_targets:
        code, day, volume, amount = str(row[0]), row[1], row[6], row[7]
        if (code, day) in no_trade:
            status = no_trade_stats[(code, day)]
            status["rows"] += 1
            if volume is None or amount is None or float(volume) != 0 or float(amount) != 0:
                status["positive_or_invalid"] += 1
    safe_no_trade = {key for key, status in no_trade_stats.items() if status["positive_or_invalid"] == 0}
    unresolved_no_trade = set(no_trade) - safe_no_trade
    formal_rows["1d"] = daily_targets
    for period in ("5m", "15m", "30m", "60m"):
        formal_rows[period] = [
            row for row in formal_rows[period]
            if ((str(row[1]), row[2]) in safe_no_trade)
            or (str(row[1]) == INDEX_CODE and row[2] < INDEX_START)
            or (period != "5m" and (period, str(row[1]), row[2]) in a_keys)
        ]
    formal_rows["1d"] = [r for r in formal_rows["1d"] if (str(r[0]), r[1]) in safe_no_trade or (str(r[0]) == INDEX_CODE and r[1] < INDEX_START)]
    stats = {
        "no_trade_candidates": len(no_trade),
        "no_trade_formal_safe": len(safe_no_trade),
        "no_trade_formal_conflicts_retained_for_manual_review": len(unresolved_no_trade),
        "no_trade_formal_conflict_examples": [
            {"code": k[0], "trade_date": k[1].isoformat(), **no_trade_stats[k]}
            for k in sorted(unresolved_no_trade)[:20]
        ],
        "no_trade_formal_rows_by_table": {p: len(rows) for p, rows in formal_rows.items()},
        "preindex_rows_by_table": {
            "5m": sum(1 for r in preindex_minute if r[0] == "5m"),
            "15m": sum(1 for r in preindex_minute if r[0] == "15m"),
            "30m": sum(1 for r in preindex_minute if r[0] == "30m"),
            "60m": sum(1 for r in preindex_minute if r[0] == "60m"),
            "1d": len(preindex_daily),
        },
        "a_formal_old_rows_by_table": {
            p: sum(1 for r in formal_rows[p] if (p, str(r[1]), r[2]) in a_keys)
            for p in ("15m", "30m", "60m")
        },
    }
    return formal_rows, formal_rows["1d"], safe_no_trade, stats


def _convert_minute_row(period: str, row: tuple[Any, ...]) -> list[Any]:
    # row: period,code,trade_date,datetime,open,high,low,close,volume,amount,created_at,id
    volume = row[8]
    if period == "5m" and volume is not None:
        numeric = float(volume)
        if not numeric.is_integer():
            raise RuntimeError(f"fractional 5m formal volume cannot be snapshotted: {row[1]} {row[3]} {volume}")
        volume = int(numeric)
    return [row[1], row[3], row[4], row[5], row[6], row[7], volume, row[9], row[10], row[11]]


def _create_backup_table(client, period: str) -> None:
    backup, formal = BACKUP[period], PERIOD_TABLE[period]
    if _table_exists(client, backup):
        raise RuntimeError(f"backup table already exists; refusing reuse: {backup}")
    if period == "1d":
        ddl = f"CREATE TABLE {backup} ENGINE=MergeTree ORDER BY (code,trade_date) AS SELECT * FROM {formal} LIMIT 0"
    else:
        ddl = f"CREATE TABLE {backup} ENGINE=MergeTree PARTITION BY toYYYYMM(datetime) ORDER BY (code,datetime) SETTINGS allow_nullable_key=1 AS SELECT * FROM {formal} LIMIT 0"
    client.command(ddl)


def _create_key_tables(client, formal_rows: dict[str, list[tuple[Any, ...]]], a_rows: dict[str, list[dict[str, Any]]]) -> dict[str, set[tuple[str, datetime]]]:
    key_sets: dict[str, set[tuple[str, datetime]]] = {p: set() for p in PERIOD_TABLE if p != "1d"}
    for period in key_sets:
        for row in formal_rows[period]:
            key_sets[period].add((str(row[1]), row[3]))
        if period in a_rows:
            # Replace the full existing code-day, including any unexpected timestamps.
            for row in formal_rows[period]:
                if (str(row[1]), row[2]) in {(r["code"], r["trade_date"]) for r in a_rows[period]}:
                    key_sets[period].add((str(row[1]), row[3]))
    for period, table in DELETE_KEYS.items():
        if _table_exists(client, table):
            raise RuntimeError(f"delete-key table already exists: {table}")
        client.command(f"CREATE TABLE {table} (code String,datetime DateTime) ENGINE=MergeTree ORDER BY (code,datetime)")
        items = sorted(key_sets[period])
        if items:
            client.insert(table, items, column_names=["code", "datetime"])
    return key_sets


def _write_rows(client, table: str, rows: list[list[Any]], columns: list[str], batch_size: int = 20000) -> int:
    count = 0
    for start in range(0, len(rows), batch_size):
        batch = rows[start:start + batch_size]
        client.insert(table, batch, column_names=columns,
                      settings={"max_partitions_per_insert_block": 1000, "max_threads": 4})
        count += len(batch)
    return count


def _build_plan(client, a_rows: list[dict[str, str]], nt_rows: list[dict[str, str]], no_exact: list[dict[str, str]], queue: list[dict[str, Any]]) -> dict[str, Any]:
    candidate_rows, candidate_checks = _read_candidate_rows(client, a_rows)
    # Scan current formal rows only by canonical code and compressed date ranges.
    target_rows, daily_rows, safe_no_trade, target_stats = _formal_target_rows(client, a_rows, nt_rows)
    a_key_set = {(r["period"], r["code"], date.fromisoformat(r["trade_date"])) for r in a_rows}
    no_trade_safe_set = safe_no_trade
    no_trade_all = {(r["code"], date.fromisoformat(r["trade_date"])) for r in nt_rows}
    excluded_a = excluded_no_trade = excluded_lifecycle = 0
    retained_by_reason = Counter()
    remaining = []
    for row in queue:
        day = row["trade_date"]
        key_a = (row["period"], row["code"], day)
        key_day = (row["code"], day)
        if key_a in a_key_set:
            excluded_a += 1
        elif key_day in no_trade_safe_set:
            excluded_no_trade += 1
        elif row["code"] == INDEX_CODE and day < INDEX_START:
            excluded_lifecycle += 1
        else:
            remaining.append(row)
            retained_by_reason[str(row["period"])] += 1
    if excluded_a != len(a_rows):
        raise RuntimeError(f"expected to resolve {len(a_rows)} A queue rows, got {excluded_a}")
    if len(remaining) + excluded_a + excluded_no_trade + excluded_lifecycle != len(queue):
        raise RuntimeError("queue reconciliation arithmetic failed")
    operation_tables = [*STAGE.values(), *BACKUP.values(), *DELETE_KEYS.values(), NO_TRADE_TABLE, LIFECYCLE_AUDIT,
                        "stock.pre2010_envelope_a_keys_20260923_v1"]
    collisions = [name for name in operation_tables if _table_exists(client, name)]
    candidate_counts = {p: len(candidate_rows[p]) for p in EXPECTED}
    candidate_rows_by_period = {p: len(candidate_rows[p]) for p in EXPECTED}
    backup_counts: dict[str, int] = {}
    backup_category_counts: dict[str, dict[str, int]] = {}
    for period in ("5m", "15m", "30m", "60m"):
        category = {"verified_no_trade": 0, "prepublication_index": 0, "A_replacement": 0}
        union = 0
        for row in target_rows[period]:
            code, day = str(row[1]), row[2]
            labels = []
            if (code, day) in no_trade_safe_set:
                labels.append("verified_no_trade")
            if code == INDEX_CODE and day < INDEX_START:
                labels.append("prepublication_index")
            if period != "5m" and (period, code, day) in a_key_set:
                labels.append("A_replacement")
            if len(labels) > 1:
                raise RuntimeError(f"formal backup target categories overlap: {period} {code} {day} {labels}")
            if labels:
                category[labels[0]] += 1
                union += 1
        backup_counts[period] = union
        backup_category_counts[period] = category
    backup_counts["1d"] = len(daily_rows)
    backup_category_counts["1d"] = {
        "verified_no_trade": sum(1 for r in daily_rows if (str(r[0]), r[1]) in no_trade_safe_set),
        "prepublication_index": sum(1 for r in daily_rows if str(r[0]) == INDEX_CODE and r[1] < INDEX_START),
        "A_replacement": 0,
    }
    if backup_counts["1d"] != sum(backup_category_counts["1d"].values()):
        raise RuntimeError("daily backup target category counts do not reconcile")
    plan = {
        "run_id": NEW_RUN,
        "base_run_id": BASE_RUN,
        "gate_run_id": GATE_RUN,
        "generated_at": datetime.now(SH_TZ).isoformat(timespec="seconds"),
        "policy": "A only auto-materialized; B/C stay in the manual review projection",
        "a_candidates": {"groups_by_period": dict(Counter(r["period"] for r in a_rows)),
                          "candidate_bars_by_period": candidate_counts,
                          "total_groups": len(a_rows), "total_bars": sum(candidate_rows_by_period.values()),
                          "all_revalidated_with_current_gate": True},
        "no_trade": target_stats,
        "prepublication_index": {
            "code": INDEX_CODE, "publication_date": INDEX_START.isoformat(),
            "external_no_exact_daily_days": len(no_exact),
            "formal_rows_to_remove_by_table": target_stats["preindex_rows_by_table"],
        },
        "queue": {"base_rows": len(queue), "remove_a_rows": excluded_a,
                  "remove_verified_no_trade_rows": excluded_no_trade,
                  "remove_prepublication_index_rows": excluded_lifecycle,
                  "new_manual_rows": len(remaining), "remaining_by_period": dict(retained_by_reason)},
        "backup_rows_by_table": backup_counts,
        "backup_rows_by_reason": backup_category_counts,
        "planned_tables": {"candidate": STAGE, "backup": BACKUP, "delete_keys": DELETE_KEYS,
                           "no_trade_audit": NO_TRADE_TABLE, "lifecycle_audit": LIFECYCLE_AUDIT},
        "table_name_collisions": collisions,
        "apply_allowed": not collisions,
    }
    return {"plan": plan, "candidate_rows": candidate_rows, "candidate_checks": candidate_checks,
            "formal_rows": target_rows, "daily_rows": daily_rows, "safe_no_trade": safe_no_trade,
            "no_trade_all": no_trade_all, "remaining_queue": remaining}


def _save_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _create_stage_tables(client, candidate_rows: dict[str, list[dict[str, Any]]]) -> None:
    for period, stage in STAGE.items():
        formal = PERIOD_TABLE[period]
        if _table_exists(client, stage):
            raise RuntimeError(f"candidate stage already exists: {stage}")
        client.command(f"""CREATE TABLE {stage}
            ENGINE=MergeTree PARTITION BY toYYYYMM(datetime)
            ORDER BY (code,datetime) SETTINGS allow_nullable_key=1
            AS SELECT * FROM {formal} LIMIT 0""")
        values = []
        for row in candidate_rows[period]:
            values.append([row[k] for k in MINUTE_COLUMNS])
        written = _write_rows(client, stage, values, MINUTE_COLUMNS)
        expected = EXPECTED[period] * len({(r["code"], r["trade_date"]) for r in candidate_rows[period]})
        actual, unique = client.query(f"SELECT count(),uniqExact(tuple(code,datetime)) FROM {stage}").result_rows[0]
        if int(actual) != expected or int(unique) != expected or written != expected:
            raise RuntimeError(f"stage gate failed {stage}: written={written}, actual={actual}, unique={unique}, expected={expected}")


def _target_old_minute_rows(period: str, rows: list[tuple[Any, ...]], safe_no_trade: set[tuple[str, date]], a_keys: set[tuple[str, str, date]]) -> list[tuple[Any, ...]]:
    selected = []
    for row in rows:
        code, day = str(row[1]), row[2]
        if (code, day) in safe_no_trade or (code == INDEX_CODE and day < INDEX_START) or (period, code, day) in a_keys:
            selected.append(row)
    return selected


def _apply_formal(client, data: dict[str, Any]) -> dict[str, Any]:
    plan = data["plan"]
    if not plan["apply_allowed"]:
        raise RuntimeError(f"table-name collision prevents apply: {plan['table_name_collisions']}")
    a_rows = data["candidate_rows"]
    safe_no_trade = data["safe_no_trade"]
    # Reconstitute A keys directly from validated candidate groups.
    a_keys = {(p, code, day) for p, rows in a_rows.items() for code, day in {(r["code"], r["trade_date"]) for r in rows}}
    _create_stage_tables(client, a_rows)
    progress = {"phase": "candidate_stages_validated", "at": datetime.now(SH_TZ).isoformat(), "stage_rows": plan["a_candidates"]["candidate_bars_by_period"]}
    (OUT / "progress.json").write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")

    # Preserve exact current formal rows before any mutation.
    old_rows: dict[str, list[tuple[Any, ...]]] = {}
    for period in ("5m", "15m", "30m", "60m"):
        old_rows[period] = _target_old_minute_rows(period, data["formal_rows"][period], safe_no_trade, a_keys)
    old_daily = data["daily_rows"]
    for period in ("5m", "15m", "30m", "60m", "1d"):
        _create_backup_table(client, period)
        if period == "1d":
            rows = [[v for v in r] for r in old_daily]
            written = _write_rows(client, BACKUP[period], rows,
                                  ["code", "trade_date", "open", "high", "low", "close", "volume", "amount", "amplitude", "change_pct", "change_amount", "turnover_rate", "created_at", "id"])
        else:
            rows = [_convert_minute_row(period, r) for r in old_rows[period]]
            written = _write_rows(client, BACKUP[period], rows, MINUTE_COLUMNS)
        actual = int(client.query(f"SELECT count() FROM {BACKUP[period]}").result_rows[0][0])
        if written != actual or actual != int(plan["backup_rows_by_table"][period]):
            raise RuntimeError(f"backup validation failed {period}: inserted={written}, actual={actual}, expected={plan['backup_rows_by_table'][period]}")
    progress = {"phase": "all_exact_backups_validated", "at": datetime.now(SH_TZ).isoformat(),
                "backup_rows": plan["backup_rows_by_table"]}
    (OUT / "progress.json").write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")

    key_sets = {p: set() for p in ("5m", "15m", "30m", "60m")}
    reasons: dict[tuple[str, str, datetime], str] = {}
    for period, rows in old_rows.items():
        for row in rows:
            code, day, dt = str(row[1]), row[2], row[3]
            key_sets[period].add((code, dt))
            if code == INDEX_CODE and day < INDEX_START:
                reason = "index_code_before_official_publication"
            elif (code, day) in safe_no_trade:
                reason = "confirmed_suspended_no_trade_zero_turnover"
            else:
                reason = "replaced_by_A_grade_purchase_source_candidate"
            reasons[(period, code, dt)] = reason
    for period in key_sets:
        name = DELETE_KEYS[period]
        if _table_exists(client, name):
            raise RuntimeError(f"delete key table already exists: {name}")
        client.command(f"CREATE TABLE {name} (code String,datetime DateTime) ENGINE=MergeTree ORDER BY (code,datetime)")
        if key_sets[period]:
            client.insert(name, sorted(key_sets[period]), column_names=["code", "datetime"])

    # Append no-trade and lifecycle evidence before mutations; these are not formal K-line tables.
    client.command(f"""CREATE TABLE {NO_TRADE_TABLE}
        (code String,trade_date Date,resolution LowCardinality(String),reason String,
         free_source_audit String,local_5m_bars UInt32,resolved_at DateTime('Asia/Shanghai'))
        ENGINE=MergeTree ORDER BY (trade_date,code)""")
    stats_by_no_trade = {(r["code"], date.fromisoformat(r["trade_date"])): r for r in _load_csv(NO_TRADE_CSV)}
    no_trade_records = []
    for code, day in sorted(safe_no_trade):
        formal_rows = [r for r in data["formal_rows"]["5m"] if str(r[1]) == code and r[2] == day]
        no_trade_records.append([code, day, "confirmed_suspended_no_trade_zero_turnover",
            "Baostock tradestatus=0; source and all current formal periods absent or exact zero volume/amount",
            str(NO_TRADE_CSV), len(formal_rows), datetime.now(SH_TZ)])
    if no_trade_records:
        _write_rows(client, NO_TRADE_TABLE, no_trade_records,
                    ["code", "trade_date", "resolution", "reason", "free_source_audit", "local_5m_bars", "resolved_at"])

    client.command(f"""CREATE TABLE {LIFECYCLE_AUDIT}
        (period LowCardinality(String),code String,trade_date Date,datetime DateTime,
         resolution LowCardinality(String),source_ref String,resolved_at DateTime('Asia/Shanghai'))
        ENGINE=MergeTree ORDER BY (period,trade_date,code,datetime)""")
    lifecycle_rows = []
    for period, rows in old_rows.items():
        for row in rows:
            code, day, dt = str(row[1]), row[2], row[3]
            if code == INDEX_CODE and day < INDEX_START:
                lifecycle_rows.append([period, code, day, dt, "removed_prepublication_index_data", str(NO_EXACT_CSV), datetime.now(SH_TZ)])
    for row in old_daily:
        code, day = str(row[0]), row[1]
        if code == INDEX_CODE and day < INDEX_START:
            lifecycle_rows.append(["1d", code, day, datetime.combine(day, datetime.min.time()), "removed_prepublication_index_data", str(NO_EXACT_CSV), datetime.now(SH_TZ)])
    if lifecycle_rows:
        _write_rows(client, LIFECYCLE_AUDIT, lifecycle_rows,
                    ["period", "code", "trade_date", "datetime", "resolution", "source_ref", "resolved_at"])

    # Delete exact old code/datetime keys only in affected monthly partitions.
    mutations: dict[str, Any] = {}
    for period in ("5m", "15m", "30m", "60m"):
        formal, key_table = PERIOD_TABLE[period], DELETE_KEYS[period]
        # Month list is computed from the target datetimes; keep one synchronous mutation per touched partition.
        months = sorted({dt.strftime("%Y-%m-01") for _, dt in key_sets[period]})
        completed = []
        for month_start in months:
            y, m = map(int, month_start[:7].split("-"))
            next_month = date(y + (m == 12), 1 if m == 12 else m + 1, 1)
            month_end = next_month.isoformat()
            client.command(f"""ALTER TABLE {formal} DELETE
                WHERE datetime>=toDateTime('{month_start} 00:00:00') AND datetime<toDateTime('{month_end} 00:00:00')
                  AND (code,datetime) IN
                      (SELECT code,datetime FROM {key_table}
                       WHERE datetime>=toDateTime('{month_start} 00:00:00') AND datetime<toDateTime('{month_end} 00:00:00'))
                SETTINGS mutations_sync=1""", settings={"max_execution_time": 1800, "max_threads": 4})
            completed.append(month_start[:7])
        mutations[period] = {"months": completed, "keys": len(key_sets[period])}
        if period in a_rows:
            staged = [[r[k] for k in MINUTE_COLUMNS] for r in a_rows[period]]
            _write_rows(client, formal, staged, MINUTE_COLUMNS)

    daily_keys = sorted({(str(r[0]), r[1]) for r in old_daily})
    if daily_keys:
        daily_key_table = "stock.pre2010_envelope_delete_keys_1d_20260923_v1"
        if _table_exists(client, daily_key_table):
            raise RuntimeError(f"daily delete key table exists: {daily_key_table}")
        client.command(f"CREATE TABLE {daily_key_table} (code String,trade_date Date) ENGINE=MergeTree ORDER BY (code,trade_date)")
        client.insert(daily_key_table, daily_keys, column_names=["code", "trade_date"])
        client.command(f"""ALTER TABLE stock.kline_daily DELETE
            WHERE (code,trade_date) IN (SELECT code,trade_date FROM {daily_key_table})
            SETTINGS mutations_sync=1""", settings={"max_execution_time": 1800, "max_threads": 4})
    mutations["1d"] = {"keys": len(daily_keys), "synchronous": True}
    progress = {"phase": "formal_mutations_and_A_insert_complete", "at": datetime.now(SH_TZ).isoformat(), "mutations": mutations}
    (OUT / "progress.json").write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")

    # Validate A rows by reading only the affected code/date ranges through the table primary key.
    check_dates: dict[str, set[date]] = defaultdict(set)
    for period, rows in a_rows.items():
        for row in rows:
            check_dates[row["code"]].add(row["trade_date"])
    observed: dict[str, list[tuple[Any, ...]]] = {p: [] for p in EXPECTED}
    for code, days in check_dates.items():
        rows = _scan_minute_rows_for_code(code, sorted(days))
        for row in rows:
            if row[0] in EXPECTED and (row[0], str(row[1]), row[2]) in a_keys:
                observed[row[0]].append(row)
    formal_summary = {}
    for period in EXPECTED:
        expected = sorted((r["code"], r["datetime"], float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]), float(r["volume"]), float(r["amount"])) for r in a_rows[period])
        actual = sorted((str(r[1]), r[3], float(r[4]), float(r[5]), float(r[6]), float(r[7]), float(r[8]), float(r[9])) for r in observed[period])
        if len(actual) != len(expected) or actual != expected:
            raise RuntimeError(f"formal A readback mismatch {period}: expected={len(expected)}, actual={len(actual)}")
        formal_summary[period] = {"candidate_rows": len(expected), "formal_rows": len(actual), "unique_keys": len({(r[0], r[1]) for r in actual})}

    # Check removed lifecycle and verified no-trade keys are absent.
    post_preindex = {}
    for period, table in PERIOD_TABLE.items():
        if period == "1d":
            n = client.query(f"SELECT count() FROM {table} WHERE code='{INDEX_CODE}' AND trade_date<toDate('{INDEX_START.isoformat()}')").result_rows[0][0]
        else:
            n = client.query(f"SELECT count() FROM {table} WHERE code='{INDEX_CODE}' AND datetime<toDateTime('{INDEX_START.isoformat()} 00:00:00')").result_rows[0][0]
        post_preindex[period] = int(n)
        if n:
            raise RuntimeError(f"pre-publication rows remain in {table}: {n}")

    safe_days_by_code: dict[str, set[date]] = defaultdict(set)
    for code, day in safe_no_trade:
        safe_days_by_code[code].add(day)
    post_no_trade_rows = 0
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_scan_minute_rows_for_code, code, sorted(days)) for code, days in safe_days_by_code.items()]
        for future in as_completed(futures):
            post_no_trade_rows += len(future.result())
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_scan_daily_rows_for_code, code, sorted(days)) for code, days in safe_days_by_code.items()]
        for future in as_completed(futures):
            post_no_trade_rows += len(future.result())
    if post_no_trade_rows:
        raise RuntimeError(f"zero-turnover formal rows remain for cleared suspension days: {post_no_trade_rows}")
    no_trade_audit_count, no_trade_audit_unique = client.query(
        f"SELECT count(),uniqExact(tuple(code,trade_date)) FROM {NO_TRADE_TABLE}"
    ).result_rows[0]
    if int(no_trade_audit_count) != len(safe_no_trade) or int(no_trade_audit_unique) != len(safe_no_trade):
        raise RuntimeError(f"no-trade resolution audit readback mismatch: {no_trade_audit_count}/{no_trade_audit_unique}")

    # Materialize the next queue by copying only unresolved B/C entries.
    if int(client.query(f"SELECT count() FROM {QUEUE} WHERE run_id='{NEW_RUN}'").result_rows[0][0]):
        raise RuntimeError(f"new review run already exists: {NEW_RUN}")
    removed_a = {(p, code, day) for p, rows in a_rows.items() for code, day in {(r["code"], r["trade_date"]) for r in rows}}
    queue_source = _scan_review_queue(client)
    next_rows = []
    removed = Counter()
    for row in queue_source:
        code, day, period = str(row["code"]), row["trade_date"], str(row["period"])
        if (period, code, day) in removed_a:
            removed["A_promoted"] += 1
            continue
        if (code, day) in safe_no_trade:
            removed["confirmed_no_trade"] += 1
            continue
        if code == INDEX_CODE and day < INDEX_START:
            removed["prepublication_index"] += 1
            continue
        copied = dict(row)
        copied["run_id"] = NEW_RUN
        next_rows.append([copied[column] for column in QUEUE_COLUMNS])
    if len(next_rows) + sum(removed.values()) != len(queue_source):
        raise RuntimeError("final queue count reconciliation failed")
    _write_rows(client, QUEUE, next_rows, QUEUE_COLUMNS, batch_size=5000)
    queue_counts = client.query(f"SELECT period,issue_type,count() FROM {QUEUE} WHERE run_id='{NEW_RUN}' GROUP BY period,issue_type ORDER BY period,issue_type").result_rows
    queue_rows = sum(int(r[2]) for r in queue_counts)
    if queue_rows != len(next_rows):
        raise RuntimeError(f"new queue readback mismatch: expected={len(next_rows)} actual={queue_rows}")
    if int(client.query(f"SELECT count() FROM {QUEUE} WHERE run_id='{NEW_RUN}' AND (code='{INDEX_CODE}' AND trade_date<toDate('{INDEX_START.isoformat()}'))").result_rows[0][0]):
        raise RuntimeError("pre-publication index review rows remain in new queue")

    issue_counts = dict(Counter())
    period_issue_counts = {f"{r[0]}:{r[1]}": int(r[2]) for r in queue_counts}
    issue_counter: Counter[str] = Counter()
    for period, issue_type, count in queue_counts:
        issue_counter[str(issue_type)] += int(count)
    issue_counts = dict(issue_counter)
    active = json.loads(ACTIVE_MANIFEST.read_text(encoding="utf-8"))
    manifest_backup = ACTIVE_MANIFEST.with_name(f"data_review_manifest.pre_envelope_closeout_20260923.json")
    if not manifest_backup.exists():
        manifest_backup.write_bytes(ACTIVE_MANIFEST.read_bytes())
    active.update({
        "run_id": NEW_RUN,
        "old_run": BASE_RUN,
        "generated_at": datetime.now(SH_TZ).isoformat(timespec="seconds"),
        "rows": queue_rows,
        "issue_type_counts": issue_counts,
        "period_issue_type_counts": period_issue_counts,
        "formal_table_mutation": True,
        "source_runs": list(dict.fromkeys(list(active.get("source_runs", [])) + [GATE_RUN, "pre2010_gate_blockers_20260923_v1", "pre2010_envelope_a_closeout_20260923_v1"])),
        "resolution": {"A_promoted": removed["A_promoted"], "confirmed_no_trade": removed["confirmed_no_trade"],
                       "prepublication_index": removed["prepublication_index"], "B_C_manual": True},
    })
    tmp = ACTIVE_MANIFEST.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(active, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(ACTIVE_MANIFEST)
    report = {
        "status": "completed", "plan": plan, "formal_readback": formal_summary,
        "prepublication_rows_remaining": post_preindex, "removed_queue_rows": dict(removed),
        "new_active_run": NEW_RUN, "new_active_rows": queue_rows, "issue_type_counts": issue_counts,
        "period_issue_type_counts": period_issue_counts,
        "backup_tables": BACKUP, "backup_rows": plan["backup_rows_by_table"],
        "candidate_tables": STAGE, "completed_at": datetime.now(SH_TZ).isoformat(timespec="seconds"),
    }
    (OUT / "operation_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "progress.json").write_text(json.dumps({"phase": "complete", "at": report["completed_at"]}, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="apply the fully revalidated plan; default is read-only")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    client = clickhouse_client()
    a_rows, nt_rows, no_exact, queue = _check_audit_and_manifest(client)
    data = _build_plan(client, a_rows, nt_rows, no_exact, queue)
    plan = data["plan"]
    _save_csv(OUT / "a_candidate_gate_checks.csv", data["candidate_checks"])
    (OUT / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    _save_csv(OUT / "manual_queue_preview.csv", [
        {"code": r["code"], "trade_date": r["trade_date"].isoformat(), "period": r["period"],
         "issue_type": r["issue_type"], "reason": r["reason"]}
        for r in data["remaining_queue"]
    ])
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    if not args.apply:
        return 0
    if not plan["apply_allowed"]:
        raise SystemExit(f"refusing apply due to table-name collisions: {plan['table_name_collisions']}")
    report = _apply_formal(client, data)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

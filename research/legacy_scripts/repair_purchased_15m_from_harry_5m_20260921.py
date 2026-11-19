"""Rebuild anomalous purchased 15m bars from the independent Harry 5m archive.

The repair is deliberately two-phase: it materializes an isolated candidate and
audit first, and only promotes rows that have a complete 48-bar Harry day and
pass the existing purchased-daily gates.  Formal promotion is opt-in and keeps
the replaced rows in a backup table before deleting the scoped formal keys.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zipfile import ZipFile

import clickhouse_connect

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.paths import runtime_path

ARCHIVE_ROOT = Path(r"E:\AStockHistory\harry")
AUDIT_SOURCE = "stock.purchased_native_15m_candidate_user_policy_20260921_day_audit"
DAILY = "stock.purchased_daily_user_policy_compare_2000_2010"
FORMAL = "stock.kline_minute_15"
CANDIDATE = "stock.purchased_native_15m_harry_repair_candidate_20260921"
REPAIR_AUDIT = "stock.purchased_native_15m_harry_repair_candidate_20260921_day_audit"
BACKUP = "stock.kline_minute_15_repair_backup_20260921"
PRICE_TOL = 0.005
VOLUME_TOL = 0.001
AMOUNT_TOL = 0.01
EXPECTED_5M = [(9, m) for m in range(35, 60, 5)] + [(10, m) for m in range(0, 60, 5)] + [(11, m) for m in range(0, 35, 5)] + [(13, m) for m in range(5, 60, 5)] + [(14, m) for m in range(0, 60, 5)] + [(15, 0)]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--codes", default="", help="comma-separated full codes; default is all review_daily_diff keys")
    p.add_argument("--keys", default="", help="comma-separated CODE=YYYY-MM-DD keys; narrows --codes for a pilot")
    p.add_argument("--exclude-keys", default="", help="comma-separated CODE=YYYY-MM-DD keys already repaired")
    p.add_argument("--batch-id", default="purchased_15m_harry_repair_20260921")
    p.add_argument("--years", default="2000-2010")
    p.add_argument("--write-candidate", action="store_true")
    p.add_argument("--promote-formal", action="store_true")
    p.add_argument("--limit-days", type=int, default=0)
    return p.parse_args()


def parse_years(value: str) -> list[int]:
    if "-" in value:
        a, b = value.split("-", 1)
        return list(range(int(a), int(b) + 1))
    return [int(x) for x in value.split(",") if x.strip()]


def client():
    return clickhouse_connect.get_client(
        host=os.getenv("AISTOCK_CLICKHOUSE_HOST", "127.0.0.1"),
        port=int(os.getenv("AISTOCK_CLICKHOUSE_PORT", "8123")),
        username=os.getenv("AISTOCK_CLICKHOUSE_USER", "default"),
        password=os.getenv("AISTOCK_CLICKHOUSE_PASSWORD", ""),
        database=os.getenv("AISTOCK_CLICKHOUSE_DATABASE", "stock"),
    )


def create_tables(c) -> None:
    c.command(f"""
    CREATE TABLE IF NOT EXISTS {CANDIDATE} (
      code String, datetime DateTime('Asia/Shanghai'), open Float64, high Float64,
      low Float64, close Float64, volume Float64, amount Float64,
      volume_unit LowCardinality(String), amount_unit LowCardinality(String),
      source_kind LowCardinality(String), source_priority UInt8, provenance String,
      source_frequency LowCardinality(String), source_year UInt16, trade_date Date,
      input_bars UInt16, quality_status LowCardinality(String), repair_reason String,
      batch_id String, created_at DateTime('Asia/Shanghai'), id UInt64
    ) ENGINE=MergeTree PARTITION BY toYYYYMM(datetime) ORDER BY (code,datetime)
    """)
    c.command(f"""
    CREATE TABLE IF NOT EXISTS {REPAIR_AUDIT} (
      source_year UInt16, code String, trade_date Date, source_5m_rows UInt16,
      source_5m_complete UInt8, candidate_bars UInt16, daily_exists UInt8,
      candidate_volume Float64, daily_volume Float64, candidate_amount Float64,
      daily_amount Float64, candidate_close Float64, daily_close Float64,
      volume_relative_diff Float64, amount_relative_diff Float64,
      price_relative_diff Float64, repair_status LowCardinality(String),
      repair_reason String, source_archive String, source_entry String,
      batch_id String, created_at DateTime('Asia/Shanghai')
    ) ENGINE=MergeTree ORDER BY (source_year,code,trade_date,created_at)
    """)


def target_keys(c, years: list[int], codes: set[str], exact_keys: set[tuple[str, date]], exclude_keys: set[tuple[str, date]], limit_days: int) -> dict[int, dict[str, set[date]]]:
    out: dict[int, dict[str, set[date]]] = defaultdict(lambda: defaultdict(set))
    for year in years:
        q = f"""SELECT source_year, code, trade_date
                 FROM {AUDIT_SOURCE}
                 WHERE source_year=%(year)s AND audit_status='review_daily_diff'"""
        rows = c.query(q, parameters={"year": year}).result_rows
        for y, code, day in rows:
            code = str(code)
            if codes and code not in codes:
                continue
            if exact_keys and (code, day) not in exact_keys:
                continue
            if (code, day) in exclude_keys:
                continue
            out[int(y)][code].add(day)
    if limit_days:
        flat = [(y, code, day) for y, by_code in out.items() for code, days in by_code.items() for day in sorted(days)]
        keep = set(flat[:limit_days])
        out = defaultdict(lambda: defaultdict(set))
        for y, code, day in keep:
            out[y][code].add(day)
    return out


def source_member_map(z: ZipFile) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in z.namelist():
        base = name.rsplit("/", 1)[-1]
        if base.lower().endswith(".csv"):
            result[base.lower()] = name
    return result


def read_code_days(z: ZipFile, member: str, wanted: set[date]) -> dict[date, dict[datetime, tuple[float, ...]]]:
    raw = z.read(member)
    text = None
    for enc in ("utf-8-sig", "gb18030", "gbk"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        return {}
    out: dict[date, dict[datetime, tuple[float, ...]]] = defaultdict(dict)
    for cells in csv.reader(text.splitlines()[1:]):
        if len(cells) < 9:
            continue
        try:
            dt = datetime.strptime(cells[0].strip(), "%Y-%m-%d %H:%M:%S")
            if dt.date() not in wanted or (dt.hour, dt.minute) not in EXPECTED_5M:
                continue
            # Harry contract: open, close, high, low, volume(lots), amount(yuan).
            values = (float(cells[3]), float(cells[5]), float(cells[6]), float(cells[4]), float(cells[7]), float(cells[8]))
            if any(v < 0 for v in values) or any(v <= 0 for v in values[:4]):
                continue
            out[dt.date()][dt] = values
        except (TypeError, ValueError, IndexError):
            continue
    return out


def aggregate_15m(code: str, day: date, bars: dict[datetime, tuple[float, ...]]) -> list[tuple]:
    ordered = [bars[datetime(day.year, day.month, day.day, h, m)] for h, m in EXPECTED_5M if datetime(day.year, day.month, day.day, h, m) in bars]
    dts = [datetime(day.year, day.month, day.day, h, m) for h, m in EXPECTED_5M if datetime(day.year, day.month, day.day, h, m) in bars]
    if len(ordered) != 48 or len(set(dts)) != 48:
        return []
    rows = []
    for i in range(0, 48, 3):
        x = ordered[i : i + 3]
        end_dt = dts[i + 2]
        rows.append((code, end_dt, x[0][0], max(v[1] for v in x), min(v[2] for v in x), x[-1][3], sum(v[4] for v in x), sum(v[5] for v in x)))
    return rows


def rel(a: float, b: float) -> float:
    return abs(a - b) / max(abs(b), 1.0)


def stable_id(code: str, dt: datetime, batch: str) -> int:
    return int.from_bytes(hashlib.blake2b(f"{code}|{dt:%Y-%m-%d %H:%M:%S}|15m|{batch}".encode(), digest_size=8).digest(), "big")


def main() -> int:
    args = parse_args()
    years = parse_years(args.years)
    codes = {x.strip().upper() for x in args.codes.split(",") if x.strip()}
    exact_keys: set[tuple[str, date]] = set()
    for item in (x.strip() for x in args.keys.split(",") if x.strip()):
        code, day = item.split("=", 1)
        exact_keys.add((code.upper(), date.fromisoformat(day)))
    exclude_keys: set[tuple[str, date]] = set()
    for item in (x.strip() for x in args.exclude_keys.split(",") if x.strip()):
        code, day = item.split("=", 1)
        exclude_keys.add((code.upper(), date.fromisoformat(day)))
    c = client()
    keys = target_keys(c, years, codes, exact_keys, exclude_keys, args.limit_days)
    batch = args.batch_id
    if args.write_candidate:
        create_tables(c)
    daily: dict[tuple[str, date], tuple[float, float, float]] = {}
    for year in years:
        rows = c.query(
            f"SELECT code,trade_date,close,volume,amount FROM {DAILY} WHERE trade_date >= %(start)s AND trade_date <= %(end)s",
            parameters={"start": date(year, 1, 1), "end": date(year, 12, 31)},
        ).result_rows
        wanted_codes = set(keys.get(year, {}))
        for code, day, close, volume, amount in rows:
            if str(code) in wanted_codes:
                daily[(str(code), day)] = (float(close or 0), float(volume or 0), float(amount or 0))

    candidate_rows: list[list] = []
    audit_rows: list[list] = []
    by_year_summary: dict[str, dict[str, int]] = {}
    for year in years:
        by_code = keys.get(year, {})
        summary = defaultdict(int)
        archive = ARCHIVE_ROOT / f"{year}_5min.zip"
        if not archive.exists():
            for code, days in by_code.items():
                for day in days:
                    audit_rows.append([year, code, day, 0, 0, 0, int((code, day) in daily), 0, daily.get((code, day), (0, 0, 0))[1], 0, daily.get((code, day), (0, 0, 0))[2], 0, daily.get((code, day), (0, 0, 0))[0], 0, 0, 0, "review", "harry_archive_missing", str(archive), "", batch, datetime.now()])
            continue
        with ZipFile(archive) as z:
            members = source_member_map(z)
            for code, days in by_code.items():
                prefix = code.split(".")[1].lower() + code.split(".")[0]
                member = members.get(f"{prefix}_{year}.csv".lower())
                parsed = read_code_days(z, member, days) if member else {}
                for day in sorted(days):
                    source_bars = parsed.get(day, {})
                    rows = aggregate_15m(code, day, source_bars)
                    dclose, dvol, damt = daily.get((code, day), (0.0, 0.0, 0.0))
                    cvol = sum(r[6] for r in rows)
                    camt = sum(r[7] for r in rows)
                    cclose = rows[-1][5] if rows else 0.0
                    vd, ad, pd = rel(cvol, dvol) if dvol else 0.0, rel(camt, damt) if damt else 0.0, rel(cclose, dclose) if dclose else 0.0
                    passed = bool(rows and dclose and dvol and damt and vd <= VOLUME_TOL and ad <= AMOUNT_TOL and pd <= PRICE_TOL)
                    status = "pass_exact_5m_reaggregate" if passed else ("review_daily_diff" if rows else "review_harry_5m_incomplete")
                    reason = "exact_5m_reaggregate" if passed else ("harry_5m_daily_diff" if rows else "harry_5m_missing_or_incomplete")
                    summary[status] += 1
                    audit_rows.append([year, code, day, len(source_bars), int(len(source_bars) == 48), len(rows), int((code, day) in daily), cvol, dvol, camt, damt, cclose, dclose, vd, ad, pd, status, reason, str(archive), member or "", batch, datetime.now()])
                    if passed:
                        for _, dt, op, hi, lo, cl, vol, amt in rows:
                            candidate_rows.append([code, dt, op, hi, lo, cl, vol, amt, "lots", "yuan", "harry_5m_aggregate", 1, f"{archive}|{member}|{day}", "15m", year, day, 3, "repair_pass", reason, batch, datetime.now(), stable_id(code, dt, batch)])
        by_year_summary[str(year)] = dict(summary)

    if args.write_candidate and candidate_rows:
        c.command("SET max_partitions_per_insert_block=1000")
        c.insert(CANDIDATE, candidate_rows, column_names=["code","datetime","open","high","low","close","volume","amount","volume_unit","amount_unit","source_kind","source_priority","provenance","source_frequency","source_year","trade_date","input_bars","quality_status","repair_reason","batch_id","created_at","id"])
    if args.write_candidate and audit_rows:
        c.insert(REPAIR_AUDIT, audit_rows, column_names=["source_year","code","trade_date","source_5m_rows","source_5m_complete","candidate_bars","daily_exists","candidate_volume","daily_volume","candidate_amount","daily_amount","candidate_close","daily_close","volume_relative_diff","amount_relative_diff","price_relative_diff","repair_status","repair_reason","source_archive","source_entry","batch_id","created_at"])

    promoted = 0
    if args.promote_formal:
        if not args.write_candidate:
            raise SystemExit("--promote-formal requires --write-candidate")
        if any(str(row[16]) != "pass_exact_5m_reaggregate" for row in audit_rows):
            raise SystemExit("refusing formal promotion: at least one requested key did not pass the repair gate")
        c.command(f"CREATE TABLE IF NOT EXISTS {BACKUP} AS {FORMAL}")
        key_days = sorted({(str(row[1]), row[2]) for row in audit_rows})
        for code, day in key_days:
            c.command(f"INSERT INTO {BACKUP} SELECT * FROM {FORMAL} WHERE code=%(code)s AND toDate(datetime)=%(day)s", parameters={"code": code, "day": day})
            c.command(f"ALTER TABLE {FORMAL} DELETE WHERE code=%(code)s AND toDate(datetime)=%(day)s SETTINGS mutations_sync=1", parameters={"code": code, "day": day})
        c.command(f"""INSERT INTO {FORMAL} (code,datetime,open,high,low,close,volume,amount,created_at,id)
          SELECT code,datetime,open,high,low,close,volume,amount,created_at,id FROM {CANDIDATE}
          WHERE batch_id=%(batch)s AND quality_status='repair_pass'""", parameters={"batch": batch})
        promoted = len(candidate_rows)

    out = runtime_path("quality_plan", batch)
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps({"batch_id": batch, "years": by_year_summary, "candidate_rows": len(candidate_rows), "audit_rows": len(audit_rows), "formal_promoted_rows": promoted}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"batch_id": batch, "years": by_year_summary, "candidate_rows": len(candidate_rows), "audit_rows": len(audit_rows), "formal_promoted_rows": promoted}, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

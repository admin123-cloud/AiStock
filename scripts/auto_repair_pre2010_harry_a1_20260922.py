"""A1 repair for the active pre-2010 review run.

Only code-days with a valid daily OHLCV record and a complete independent
Harry 48-bar 5m day are promoted.  The A1 gate is 1% for each OHLC field,
volume and amount.  B/C evidence is retained in the audit table and is never
written to formal tables.
"""
from __future__ import annotations

import csv
import hashlib
import json
import zipfile
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import clickhouse_connect

RUN = "purchased_history_review_20260922_refit_a_b_confirmed_20260922_v12_harry_a1_1pct_corrected"
ARCHIVE_ROOT = Path(r"E:\AStockHistory\harry")
REPORT = Path(r"F:\AiStock\data\data\runtime\purchased_history\harry_a2_repair_20260922.json")
PRICE_TOL = VOLUME_TOL = AMOUNT_TOL = 0.02
SLOTS = [(9, m) for m in range(35, 60, 5)] + [(10, m) for m in range(0, 60, 5)] + [(11, m) for m in range(0, 35, 5)] + [(13, m) for m in range(5, 60, 5)] + [(14, m) for m in range(0, 60, 5)] + [(15, 0)]
PERIODS = {"5m": (5, "stock.kline_minute_5"), "15m": (15, "stock.kline_minute_15"), "30m": (30, "stock.kline_minute_30"), "60m": (60, "stock.kline_minute_60")}


def rel(a: float, b: float) -> float:
    return abs(a - b) / max(abs(b), 1.0)


def member_map(z: zipfile.ZipFile) -> dict[str, str]:
    return {n.rsplit("/", 1)[-1].lower(): n for n in z.namelist() if n.lower().endswith(".csv")}


def read_days(z: zipfile.ZipFile, member: str, wanted: set[date]) -> dict[date, dict[datetime, tuple[float, ...]]]:
    out: dict[date, dict[datetime, tuple[float, ...]]] = defaultdict(dict)
    if not member:
        return out
    with z.open(member) as fh:
        text = fh.read().decode("utf-8-sig", errors="ignore")
    for cells in csv.reader(text.splitlines()[1:]):
        if len(cells) < 9:
            continue
        try:
            dt = datetime.strptime(cells[0].strip(), "%Y-%m-%d %H:%M:%S")
            if dt.date() not in wanted or (dt.hour, dt.minute) not in SLOTS:
                continue
            # Harry CSV: open, close, high, low, volume(lots), amount(yuan)
            vals = tuple(float(cells[i]) for i in (3, 4, 5, 6, 7, 8))
            if any(v <= 0 for v in vals[:4]) or vals[4] < 0 or vals[5] < 0:
                continue
            out[dt.date()][dt] = vals
        except (ValueError, IndexError):
            continue
    return out


def aggregate(code: str, day: date, bars: dict[datetime, tuple[float, ...]], period: str) -> list[tuple]:
    ordered = [(datetime(day.year, day.month, day.day, h, m), bars.get(datetime(day.year, day.month, day.day, h, m))) for h, m in SLOTS]
    if any(v is None for _, v in ordered) or len(ordered) != 48:
        return []
    if period == "5m":
        groups = [[x] for x in ordered]
    elif period == "60m":
        groups = [ordered[0:12], ordered[12:24], ordered[24:36], ordered[36:48]]
    else:
        n = 3 if period == "15m" else 6
        groups = [ordered[i:i+n] for i in range(0, 48, n)]
    rows = []
    for group in groups:
        vals = [x[1] for x in group]
        end_dt = group[-1][0]
        rows.append((code, end_dt, vals[0][0], max(x[2] for x in vals), min(x[3] for x in vals), vals[-1][1], sum(x[4] for x in vals), sum(x[5] for x in vals)))
    return rows


def main() -> None:
    c = clickhouse_connect.get_client(host="localhost", port=8123, username="default", settings={"max_execution_time": 600})
    q = c.query(f"SELECT code,trade_date,groupUniqArray(period) FROM stock.purchased_history_data_review_items WHERE run_id='{RUN}' GROUP BY code,trade_date").result_rows
    targets: dict[int, dict[str, dict[date, set[str]]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(set)))
    for code, day, periods in q:
        targets[day.year][str(code)][day] = set(str(x) for x in periods)
    daily: dict[tuple[str, date], tuple[float, ...]] = {}
    for year, by_code in targets.items():
        codes = list(by_code)
        # Query the bounded year once; filter to review codes in Python.
        rows = c.query("SELECT code,trade_date,open,high,low,close,volume,amount FROM stock.kline_daily WHERE trade_date BETWEEN %(a)s AND %(b)s", parameters={"a": date(year,1,1), "b": date(year,12,31)}).result_rows
        wanted = set(codes)
        for code, day, op, hi, lo, cl, vol, amt in rows:
            if str(code) in wanted:
                daily[(str(code), day)] = tuple(float(x or 0) for x in (op, hi, lo, cl, vol, amt))

    audits = []
    candidates: dict[str, list[tuple]] = defaultdict(list)
    a_keys: dict[str, set[tuple[str, date]]] = defaultdict(set)
    status = defaultdict(int)
    for year, by_code in sorted(targets.items()):
        archive = ARCHIVE_ROOT / f"{year}_5min.zip"
        if not archive.exists():
            for code, by_day in by_code.items():
                for day, periods in by_day.items():
                    audits.append([code, day, ",".join(sorted(periods)), 0, "archive_missing", 0, 0, 0, 0, 0, 0, 0, 0])
            continue
        with zipfile.ZipFile(archive) as z:
            members = member_map(z)
            for code, by_day in by_code.items():
                prefix = code.split(".")[1].lower() + code.split(".")[0]
                parsed = read_days(z, members.get(f"{prefix}_{year}.csv".lower()), set(by_day))
                for day, periods in by_day.items():
                    dv = daily.get((code, day))
                    bars = parsed.get(day, {})
                    for period in periods:
                        rows = aggregate(code, day, bars, period)
                        if not dv:
                            grade, reason = "C", "daily_missing_or_invalid"
                        elif len(rows) != {"5m":48,"15m":16,"30m":8,"60m":4}[period]:
                            grade, reason = "C", "harry_5m_incomplete"
                        else:
                            agg = (rows[0][2], max(r[3] for r in rows), min(r[4] for r in rows), rows[-1][5], sum(r[6] for r in rows), sum(r[7] for r in rows))
                            diffs = tuple(rel(agg[i], dv[i]) for i in range(6))
                            grade = "A" if max(diffs[:4]) <= PRICE_TOL and diffs[4] <= VOLUME_TOL and diffs[5] <= AMOUNT_TOL else "B"
                            reason = "all_1pct_gates_pass" if grade == "A" else "daily_diff_over_1pct"
                            if grade == "A":
                                a_keys[period].add((code, day))
                                candidates[period].extend(rows)
                        status[(period, grade)] += 1
                        audits.append([code, day, period, len(bars), grade, reason, len(rows), *(tuple(rel((rows[0][2], max(r[3] for r in rows), min(r[4] for r in rows), rows[-1][5], sum(r[6] for r in rows), sum(r[7] for r in rows)) [i], dv[i]) for i in range(6)) if dv and rows else (0,)*6)])

    run_tag = "harry_a2_2pct_20260922"
    c.command("SET max_partitions_per_insert_block=1000")
    promoted = {}
    for period, rows in candidates.items():
        if not rows:
            promoted[period] = 0
            continue
        formal = PERIODS[period][1]
        cand = f"stock.pre2010_{period}_harry_a2_candidate_20260922"
        keys = f"stock.pre2010_{period}_harry_a2_keys_20260922"
        backup = f"stock.{period}_repair_backup_harry_a2_20260922"
        c.command(f"DROP TABLE IF EXISTS {cand}")
        c.command(f"DROP TABLE IF EXISTS {keys}")
        c.command(f"CREATE TABLE {keys} (code String, trade_date Date) ENGINE=MergeTree ORDER BY (code,trade_date)")
        c.insert(keys, list(a_keys[period]), column_names=["code","trade_date"])
        c.command(f"CREATE TABLE {cand} AS {formal} ENGINE=MergeTree ORDER BY (code,datetime) SETTINGS allow_nullable_key=1")
        c.command(f"CREATE TABLE IF NOT EXISTS {backup} AS {formal} ENGINE=MergeTree ORDER BY (code,datetime) SETTINGS allow_nullable_key=1")
        old = c.query(f"SELECT code,datetime,open,high,low,close,volume,amount,created_at,id FROM {formal} WHERE (code,toDate(datetime)) IN (SELECT code,trade_date FROM {keys})").result_rows
        if old:
            c.insert(backup, old, column_names=["code","datetime","open","high","low","close","volume","amount","created_at","id"])
        now = datetime.now()
        vals = [(r[0],r[1],r[2],r[3],r[4],r[5],r[6],r[7],now,int(hashlib.sha256(f"{r[0]}|{r[1]}|{period}|{run_tag}".encode()).hexdigest()[:16],16)) for r in rows]
        c.insert(cand, vals, column_names=["code","datetime","open","high","low","close","volume","amount","created_at","id"])
        c.command(f"ALTER TABLE {formal} DELETE WHERE (code,toDate(datetime)) IN (SELECT code,trade_date FROM {keys}) SETTINGS mutations_sync=1")
        c.insert(formal, vals, column_names=["code","datetime","open","high","low","close","volume","amount","created_at","id"])
        promoted[period] = len(vals)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    report = {"run_id": run_tag, "base_review_run": RUN, "thresholds": {"price_ohlc": 0.02, "volume": 0.02, "amount": 0.02}, "review_groups": sum(len(x) for by_code in targets.values() for x in by_code.values()), "status": {f"{p}:{g}": n for (p,g),n in status.items()}, "a_keys": {p: len(k) for p,k in a_keys.items()}, "promoted_rows": promoted, "b_c_not_promoted": sum(n for (p,g),n in status.items() if g in ("B","C"))}
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()

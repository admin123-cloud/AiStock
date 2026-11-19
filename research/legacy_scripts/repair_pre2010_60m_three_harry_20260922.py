from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import date, datetime
from pathlib import Path

import clickhouse_connect
import pandas as pd


DATA_ROOT = Path(r"E:\AStockHistory\harry")
REPORT = Path(r"F:\AiStock\data\data\runtime\purchased_history\60m_harry_three_repair_20260922.json")
FORMAL = "stock.kline_minute_60"
CAND = "stock.pre2010_60m_harry_confirmed_20260922"
KEYS = "stock.pre2010_60m_harry_keys_20260922"
BACKUP = "stock.kline_minute_60_repair_backup_harry_20260922"
TARGETS = [
    ("000915.SZ", "2003", "2003-07-03", "sz000915"),
    ("000733.SZ", "2003", "2003-07-03", "sz000733"),
    ("600511.SH", "2006", "2006-01-09", "sh600511"),
]


def read_day(year: str, day: str, symbol: str) -> pd.DataFrame:
    with zipfile.ZipFile(DATA_ROOT / f"{year}_5min.zip") as zf:
        name = f"{symbol}_{year}.csv"
        with zf.open(name) as fh:
            raw = pd.read_csv(fh, encoding="utf-8-sig")
    cols = ["datetime", "source_code", "name", "open", "close", "high", "low", "volume", "amount", "change_pct", "amplitude"]
    raw.columns = cols[: len(raw.columns)]
    raw["datetime"] = pd.to_datetime(raw["datetime"])
    raw = raw[raw["datetime"].dt.strftime("%Y-%m-%d") == day].sort_values("datetime").copy()
    if len(raw) != 48:
        raise RuntimeError(f"{symbol} {day}: expected 48 5m bars, got {len(raw)}")
    for c in ["open", "close", "high", "low", "volume", "amount"]:
        raw[c] = pd.to_numeric(raw[c], errors="coerce")
    return raw


def bucket(ts: pd.Timestamp) -> str:
    m = ts.hour * 60 + ts.minute
    if m <= 625:
        return "10:30:00"
    if m <= 685:
        return "11:30:00"
    if m <= 835:
        return "14:00:00"
    return "15:00:00"


def aggregate(code: str, day: str, raw: pd.DataFrame) -> list[dict]:
    raw["bucket"] = raw["datetime"].map(bucket)
    rows = []
    for label, grp in raw.groupby("bucket", sort=False):
        ts = pd.Timestamp(f"{day} {label}")
        key = f"{code}|{ts:%Y-%m-%d %H:%M:%S}|harry"
        rows.append({
            "code": code,
            "datetime": ts.to_pydatetime(),
            "open": float(grp.iloc[0]["open"]),
            "high": float(grp["high"].max()),
            "low": float(grp["low"].min()),
            "close": float(grp.iloc[-1]["close"]),
            "volume": int(round(grp["volume"].sum())),
            "amount": float(grp["amount"].sum()),
            "created_at": datetime.now(),
            "id": int(hashlib.sha256(key.encode()).hexdigest()[:16], 16),
        })
    return rows


def main() -> None:
    client = clickhouse_connect.get_client(host="localhost", port=8123, username="default")
    client.command("SET max_partitions_per_insert_block=1000")
    all_rows: list[dict] = []
    source_checks = []
    for code, year, day, symbol in TARGETS:
        raw = read_day(year, day, symbol)
        rows = aggregate(code, day, raw)
        all_rows.extend(rows)
        source_checks.append({
            "code": code,
            "trade_date": day,
            "bars_5m": len(raw),
            "volume_5m": int(round(raw["volume"].sum())),
            "amount_5m": float(raw["amount"].sum()),
            "open": float(raw.iloc[0]["open"]),
            "high": float(raw["high"].max()),
            "low": float(raw["low"].min()),
            "close": float(raw.iloc[-1]["close"]),
        })

    client.command(f"DROP TABLE IF EXISTS {CAND}")
    client.command(f"DROP TABLE IF EXISTS {KEYS}")
    client.command(f"DROP TABLE IF EXISTS {BACKUP}")
    client.command(f"CREATE TABLE {KEYS} (code String, trade_date Date) ENGINE=MergeTree ORDER BY (code, trade_date)")
    client.insert(KEYS, [(x[0], date.fromisoformat(x[2])) for x in TARGETS], column_names=["code", "trade_date"])
    client.command(f"CREATE TABLE {CAND} AS {FORMAL} ENGINE=MergeTree ORDER BY (code, datetime) SETTINGS allow_nullable_key=1")
    client.command(f"CREATE TABLE {BACKUP} AS {FORMAL} ENGINE=MergeTree ORDER BY (code, datetime) SETTINGS allow_nullable_key=1")
    old = client.query(f"SELECT code, datetime, open, high, low, close, volume, amount, created_at, id FROM {FORMAL} WHERE (code, toDate(datetime)) IN (SELECT code, trade_date FROM {KEYS})").result_rows
    if old:
        client.insert(BACKUP, old, column_names=["code", "datetime", "open", "high", "low", "close", "volume", "amount", "created_at", "id"])
    vals = [(r["code"], r["datetime"], r["open"], r["high"], r["low"], r["close"], r["volume"], r["amount"], r["created_at"], r["id"]) for r in all_rows]
    client.insert(CAND, vals, column_names=["code", "datetime", "open", "high", "low", "close", "volume", "amount", "created_at", "id"])
    client.command(f"ALTER TABLE {FORMAL} DELETE WHERE (code, toDate(datetime)) IN (SELECT code, trade_date FROM {KEYS}) SETTINGS mutations_sync=1")
    client.insert(FORMAL, vals, column_names=["code", "datetime", "open", "high", "low", "close", "volume", "amount", "created_at", "id"])
    post = client.query(f"SELECT count(), uniqExact((code, datetime)), countIf(open<=0 OR high<=0 OR low<=0 OR close<=0 OR high<low OR volume<0 OR amount<0) FROM {FORMAL} WHERE (code, toDate(datetime)) IN (SELECT code, trade_date FROM {KEYS})").result_rows[0]
    report = {"run_id": "60m_harry_three_repair_20260922", "targets": [x[0] for x in TARGETS], "source_checks": source_checks, "candidate_rows": len(all_rows), "old_formal_rows_backed_up": len(old), "post_validation": {"rows": post[0], "unique_keys": post[1], "invalid_rows": post[2]}, "tables": {"candidate": CAND, "keys": KEYS, "backup": BACKUP, "formal": FORMAL}}
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()

"""Repair daily stock volume/amount units by reconciling local TDX day files.

The internal price/amount relation cannot detect cases where volume and
amount were both scaled by 100.  This script compares persisted stock daily
rows with local TDX `.day` bars and rewrites only mismatched keys with TDX
volume-in-lots and amount-in-yuan values.
"""

from __future__ import annotations

import argparse
import struct
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_client, clickhouse_query_df
from utils.kline_units import DAILY_UNIT_REPAIRABLE_CLASSES, classify_daily_unit_pair


DAY_RECORD = struct.Struct("<IIIIIfII")
TARGET_COLUMNS = [
    "code",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "amplitude",
    "change_pct",
    "change_amount",
    "turnover_rate",
    "created_at",
    "id",
]
STAGE_TABLE = "kline_daily_unit_repair_tdx_stage"
REPAIRABLE_CLASSES = DAILY_UNIT_REPAIRABLE_CLASSES


def _quote(value: Any) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _parse_day(raw: int) -> date | None:
    text = str(int(raw))
    if len(text) != 8:
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except ValueError:
        return None


def _full_code(path: Path, market: str) -> str:
    stem = path.stem
    raw = stem[2:] if stem[:2].lower() in {"sh", "sz", "bj"} else stem
    return f"{raw}.{market.upper()}"


def _load_tdx_values(root: Path, db_keys: set[tuple[str, date]], start_day: date, end_day: date) -> tuple[dict[tuple[str, date], tuple[float, float]], date | None]:
    values: dict[tuple[str, date], tuple[float, float]] = {}
    max_day: date | None = None
    code_set = {code for code, _day in db_keys}
    for market in ("sh", "sz", "bj"):
        folder = root / market / "lday"
        if not folder.exists():
            continue
        for path in folder.glob("*.day"):
            code = _full_code(path, market)
            if code not in code_set:
                continue
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(DAY_RECORD.size)
                    if not chunk or len(chunk) < DAY_RECORD.size:
                        break
                    raw_day, _open_i, _high_i, _low_i, _close_i, amount_f, volume_i, _reserved = DAY_RECORD.unpack(chunk)
                    trade_day = _parse_day(raw_day)
                    if trade_day is None or trade_day < start_day or trade_day > end_day:
                        continue
                    if max_day is None or trade_day > max_day:
                        max_day = trade_day
                    key = (code, trade_day)
                    if key not in db_keys:
                        continue
                    volume = round(float(volume_i or 0) / 100.0, 2)
                    amount = float(amount_f or 0.0)
                    if volume > 0 and amount > 0:
                        values[key] = (volume, amount)
    return values, max_day


def _classify(db_volume: float, db_amount: float, source_volume: float, source_amount: float) -> str:
    return classify_daily_unit_pair(db_volume, db_amount, source_volume, source_amount)


def _ensure_stage(client) -> None:
    client.command(
        f"""
        CREATE TABLE IF NOT EXISTS {STAGE_TABLE}
        (
            code String,
            trade_date Date,
            open Float64,
            high Float64,
            low Float64,
            close Float64,
            volume Float64,
            amount Float64,
            amplitude Float64,
            change_pct Float64,
            change_amount Float64,
            turnover_rate Float64,
            created_at Nullable(DateTime),
            id UInt64
        )
        ENGINE = MergeTree
        ORDER BY (code, trade_date)
        """
    )
    client.command(f"TRUNCATE TABLE {STAGE_TABLE}")


def repair_from_tdx(start_date: str, end_date: str, tdx_root: Path, apply: bool = False) -> dict[str, Any]:
    start_day = date.fromisoformat(start_date)
    end_day = date.fromisoformat(end_date)
    if start_day > end_day:
        raise ValueError("start_date must be <= end_date")
    if not tdx_root.exists():
        raise FileNotFoundError(f"TDX root not found: {tdx_root}")

    df = clickhouse_query_df(
        f"""
        SELECT {", ".join("k." + col for col in TARGET_COLUMNS)}
        FROM (SELECT * FROM kline_daily FINAL) AS k
        INNER JOIN stocks AS s ON s.code = k.code
        WHERE s.type = 'stock'
          AND k.trade_date >= toDate({_quote(start_date)})
          AND k.trade_date <= toDate({_quote(end_date)})
        """
    )
    if df.empty:
        return {"ok": False, "reason": "no_db_rows", "start_date": start_date, "end_date": end_date}

    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    db_keys = {(str(row.code), row.trade_date) for row in df.itertuples(index=False)}
    tdx_values, tdx_max_day = _load_tdx_values(tdx_root, db_keys, start_day, end_day)
    if not tdx_values:
        return {
            "ok": False,
            "reason": "no_tdx_overlap",
            "start_date": start_date,
            "end_date": end_date,
            "db_rows": int(len(df)),
        }

    checked = 0
    counts: Counter[str] = Counter()
    samples: dict[str, dict[str, Any]] = {}
    repair_rows: list[tuple[Any, ...]] = []
    now = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None, microsecond=0)
    for row in df.itertuples(index=False):
        key = (str(row.code), row.trade_date)
        source = tdx_values.get(key)
        if source is None:
            continue
        checked += 1
        source_volume, source_amount = source
        db_volume = float(row.volume or 0.0)
        db_amount = float(row.amount or 0.0)
        bucket = _classify(db_volume, db_amount, source_volume, source_amount)
        counts[bucket] += 1
        samples.setdefault(
            bucket,
            {
                "code": key[0],
                "trade_date": str(key[1]),
                "db_volume": db_volume,
                "source_volume": source_volume,
                "db_amount": db_amount,
                "source_amount": source_amount,
            },
        )
        if bucket == "ok":
            continue
        if bucket not in REPAIRABLE_CLASSES:
            continue
        item = row._asdict()
        item["volume"] = source_volume
        item["amount"] = source_amount
        item["created_at"] = now
        repair_rows.append(tuple(item[col] for col in TARGET_COLUMNS))

    summary: dict[str, Any] = {
        "ok": True,
        "dry_run": not apply,
        "start_date": start_date,
        "end_date": end_date,
        "tdx_root": str(tdx_root),
        "tdx_max_day": str(tdx_max_day) if tdx_max_day else None,
        "db_rows": int(len(df)),
        "tdx_overlap_rows": checked,
        "classification_counts": dict(sorted(counts.items())),
        "sample_by_class": samples,
        "repairable_classes": sorted(REPAIRABLE_CLASSES),
        "rows_to_repair": len(repair_rows),
    }
    if not apply or not repair_rows:
        return summary

    client = clickhouse_client()
    _ensure_stage(client)
    batch_size = 50000
    for start in range(0, len(repair_rows), batch_size):
        client.insert(STAGE_TABLE, repair_rows[start : start + batch_size], column_names=TARGET_COLUMNS)
    client.command(
        f"""
        ALTER TABLE kline_daily
        DELETE WHERE (code, trade_date) IN (
            SELECT code, trade_date FROM {STAGE_TABLE}
        )
        SETTINGS mutations_sync = 1
        """
    )
    client.command(
        f"""
        INSERT INTO kline_daily ({", ".join(TARGET_COLUMNS)})
        SELECT {", ".join(TARGET_COLUMNS)}
        FROM {STAGE_TABLE}
        """
    )
    summary["rows_repaired"] = len(repair_rows)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--tdx-root", default=r"D:\TDX\vipdoc")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = repair_from_tdx(
        args.start_date,
        args.end_date,
        Path(args.tdx_root),
        apply=args.apply,
    )
    print(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

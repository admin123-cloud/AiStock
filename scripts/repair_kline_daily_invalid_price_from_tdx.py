"""Repair daily rows with invalid OHLC from exact local TDX day matches.

This is an explicit historical repair command, separate from the recurring
daily coverage job. It only considers rows whose persisted OHLC contains a
non-positive value while volume and amount are positive. A row is repairable
only when the exact local TDX ``(code, trade_date)`` bar has valid OHLC and
its normalized volume/amount agree with the persisted values.

The command is read-only by default. Use ``--apply`` to replace the complete
OHLCV/amount row through a bounded ClickHouse stage table.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from utils.market_warehouse import clickhouse_client, clickhouse_query_df
from utils.paths import report_path


BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
DAY_RECORD = struct.Struct("<IIIIIfII")
STAGE_TABLE = "kline_daily_invalid_price_tdx_stage"
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
PRICE_TOLERANCE = 0.011
AMOUNT_RELATIVE_TOLERANCE = 0.00002
AMOUNT_ABSOLUTE_TOLERANCE = 2.0
VOLUME_RELATIVE_TOLERANCE = 0.0001
VOLUME_ABSOLUTE_TOLERANCE = 0.01


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


def _tdx_path(root: Path, code: str) -> Path:
    raw, market = str(code).upper().split(".", 1)
    return root / market.lower() / "lday" / f"{market.lower()}{raw}.day"


def _read_source(path: Path) -> dict[date, dict[str, float]]:
    result: dict[date, dict[str, float]] = {}
    if not path.exists():
        return result
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(DAY_RECORD.size)
            if not chunk or len(chunk) < DAY_RECORD.size:
                break
            raw_day, open_i, high_i, low_i, close_i, amount_f, volume_i, _reserved = DAY_RECORD.unpack(chunk)
            trade_day = _parse_day(raw_day)
            if trade_day is None:
                continue
            result[trade_day] = {
                "open": float(open_i) / 100.0,
                "high": float(high_i) / 100.0,
                "low": float(low_i) / 100.0,
                "close": float(close_i) / 100.0,
                "volume": round(float(volume_i or 0) / 100.0, 2),
                "amount": float(amount_f or 0.0),
            }
    return result


def _metrics(source_rows: dict[date, dict[str, float]], trade_day: date) -> tuple[float, float, float]:
    ordered = sorted(source_rows)
    try:
        position = ordered.index(trade_day)
    except ValueError:
        return 0.0, 0.0, 0.0
    current = source_rows[trade_day]
    if position == 0:
        return 0.0, 0.0, 0.0
    previous_close = float(source_rows[ordered[position - 1]]["close"] or 0.0)
    if previous_close <= 0:
        return 0.0, 0.0, 0.0
    change_amount = current["close"] - previous_close
    change_pct = change_amount / previous_close * 100.0
    amplitude = (current["high"] - current["low"]) / previous_close * 100.0
    return amplitude, change_pct, change_amount


def _close_enough(actual: float, expected: float, relative: float, absolute: float) -> bool:
    return abs(float(actual) - float(expected)) <= max(float(absolute), abs(float(expected)) * float(relative))


def _load_candidates() -> pd.DataFrame:
    return clickhouse_query_df(
        """
        SELECT
            k.code,
            k.trade_date,
            k.open,
            k.high,
            k.low,
            k.close,
            k.volume,
            k.amount,
            k.amplitude,
            k.change_pct,
            k.change_amount,
            k.turnover_rate,
            k.created_at,
            k.id
        FROM (SELECT * FROM kline_daily FINAL) AS k
        INNER JOIN stocks AS s ON s.code = k.code
        WHERE s.type IN ('stock', 'index')
          AND k.volume > 0
          AND k.amount > 0
          AND (k.open <= 0 OR k.high <= 0 OR k.low <= 0 OR k.close <= 0)
        ORDER BY k.code, k.trade_date
        """
    )


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


def _apply(client, rows: list[tuple[Any, ...]]) -> int:
    if not rows:
        return 0
    _ensure_stage(client)
    client.insert(STAGE_TABLE, rows, column_names=TARGET_COLUMNS)
    client.command(
        f"""
        ALTER TABLE kline_daily
        DELETE WHERE (code, trade_date) IN (
            SELECT code, trade_date
            FROM {STAGE_TABLE}
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
    return len(rows)


def audit(args: argparse.Namespace) -> dict[str, Any]:
    frame = _load_candidates()
    if frame.empty:
        return {
            "status": "healthy",
            "mode": "apply" if args.apply else "audit",
            "candidate_rows": 0,
            "source_overlap_rows": 0,
            "validated_rows": 0,
            "applied_rows": 0,
            "rejected_counts": {},
            "samples": {},
        }

    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date
    by_code: dict[str, list[Any]] = defaultdict(list)
    for row in frame.itertuples(index=False):
        by_code[str(row.code).upper()].append(row)

    source_cache: dict[str, dict[date, dict[str, float]]] = {}
    repair_rows: list[tuple[Any, ...]] = []
    rejected: Counter[str] = Counter()
    samples: dict[str, dict[str, Any]] = {}
    source_overlap = 0
    created_at = datetime.now(BUSINESS_TZ).replace(tzinfo=None, microsecond=0)

    for code, db_rows in sorted(by_code.items()):
        path = _tdx_path(Path(args.tdx_root), code)
        source_rows = source_cache.setdefault(code, _read_source(path))
        if not source_rows:
            rejected["tdx_file_or_rows_missing"] += len(db_rows)
            samples.setdefault(
                "tdx_file_or_rows_missing",
                {"code": code, "path": str(path), "rows": len(db_rows)},
            )
            continue

        for row in db_rows:
            source = source_rows.get(row.trade_date)
            if source is None:
                rejected["tdx_bar_missing"] += 1
                samples.setdefault(
                    "tdx_bar_missing",
                    {"code": code, "trade_date": str(row.trade_date)},
                )
                continue
            source_overlap += 1
            if any(source[field] <= 0 for field in ("open", "high", "low", "close", "volume", "amount")):
                rejected["source_nonpositive"] += 1
                samples.setdefault(
                    "source_nonpositive",
                    {"code": code, "trade_date": str(row.trade_date)},
                )
                continue
            if not _close_enough(
                float(row.amount),
                source["amount"],
                AMOUNT_RELATIVE_TOLERANCE,
                AMOUNT_ABSOLUTE_TOLERANCE,
            ):
                rejected["amount_mismatch"] += 1
                samples.setdefault(
                    "amount_mismatch",
                    {
                        "code": code,
                        "trade_date": str(row.trade_date),
                        "db_amount": float(row.amount),
                        "source_amount": source["amount"],
                    },
                )
                continue
            if not _close_enough(
                float(row.volume),
                source["volume"],
                VOLUME_RELATIVE_TOLERANCE,
                VOLUME_ABSOLUTE_TOLERANCE,
            ):
                rejected["volume_mismatch"] += 1
                samples.setdefault(
                    "volume_mismatch",
                    {
                        "code": code,
                        "trade_date": str(row.trade_date),
                        "db_volume": float(row.volume),
                        "source_volume": source["volume"],
                    },
                )
                continue

            amplitude, change_pct, change_amount = _metrics(source_rows, row.trade_date)
            repair_rows.append(
                (
                    code,
                    row.trade_date,
                    source["open"],
                    source["high"],
                    source["low"],
                    source["close"],
                    source["volume"],
                    source["amount"],
                    amplitude,
                    change_pct,
                    change_amount,
                    float(row.turnover_rate or 0.0),
                    created_at,
                    int(row.id or 0),
                )
            )

    applied = _apply(clickhouse_client(), repair_rows) if args.apply and repair_rows else 0
    return {
        "status": "healthy" if not rejected else "degraded",
        "mode": "apply" if args.apply else "audit",
        "tdx_root": str(args.tdx_root),
        "candidate_rows": int(len(frame)),
        "source_overlap_rows": int(source_overlap),
        "validated_rows": int(len(repair_rows)),
        "applied_rows": int(applied),
        "rejected_counts": dict(sorted(rejected.items())),
        "samples": samples,
        "repair_contract": {
            "key": "(code, trade_date)",
            "source": "local TDX .day",
            "volume": "shares converted to lots",
            "amount": "yuan unchanged",
            "historical_multiplier_inference": False,
        },
    }


def parse_args() -> argparse.Namespace:
    now = datetime.now(BUSINESS_TZ)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tdx-root", type=Path, default=Path(r"D:\TDX\vipdoc"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--report",
        type=Path,
        default=Path(report_path("kline_daily_database_audit", f"invalid_price_{now:%Y%m%d_%H%M%S}.json")),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = audit(args)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": payload["status"],
                "mode": payload["mode"],
                "report": str(args.report),
                "candidate_rows": payload["candidate_rows"],
                "validated_rows": payload["validated_rows"],
                "applied_rows": payload["applied_rows"],
            },
            ensure_ascii=False,
        )
    )
    return 0 if payload["status"] == "healthy" or not args.apply else 2


if __name__ == "__main__":
    raise SystemExit(main())

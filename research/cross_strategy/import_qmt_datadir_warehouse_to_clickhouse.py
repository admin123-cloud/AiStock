from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from scripts.qmtmini_daily_backfill_validate import ch_client, quote_sql
from utils.paths import report_path, warehouse_path


DEFAULT_WAREHOUSE_ROOT = warehouse_path("qmt_datadir_minute_v1")
DEFAULT_REPORT = report_path("qmt_datadir_minute_import_20260708", "summary.json")
DEFAULT_STAGE_TABLE = "qmt_datadir_minute_stage"
PERIOD_TO_TABLE = {
    "5m": "kline_minute_5",
    "15m": "kline_minute_15",
    "30m": "kline_minute_30",
    "60m": "kline_minute_60",
}
TARGET_COLUMNS = ["code", "datetime", "open", "high", "low", "close", "volume", "amount", "created_at", "id"]
STAGE_COLUMNS = ["period", *TARGET_COLUMNS]


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def stable_id(period: str, code: str, dt: Any) -> int:
    ts = pd.Timestamp(dt).to_pydatetime().replace(tzinfo=None)
    key = f"{period}|{code}|{ts:%Y-%m-%d %H:%M:%S}".encode("utf-8")
    return int.from_bytes(hashlib.blake2b(key, digest_size=8).digest(), "big", signed=False)


def ensure_stage_table(client, stage_table: str, reset: bool) -> None:
    if reset:
        client.command(f"DROP TABLE IF EXISTS {stage_table}")
    client.command(
        f"""
        CREATE TABLE IF NOT EXISTS {stage_table}
        (
            period String,
            code String,
            datetime DateTime,
            open Float64,
            high Float64,
            low Float64,
            close Float64,
            volume Float64,
            amount Float64,
            created_at DateTime,
            id UInt64
        )
        ENGINE = ReplacingMergeTree(created_at)
        ORDER BY (period, code, datetime)
        SETTINGS index_granularity = 8192
        """
    )


def period_files(root: Path, period: str) -> list[Path]:
    base = root / f"period={period}"
    return sorted(base.rglob("*.parquet")) if base.exists() else []


def load_parquet_for_insert(path: Path, period: str, created_at: datetime) -> pd.DataFrame:
    df = pd.read_parquet(path)
    if df.empty:
        return df
    df = df.copy()
    df["period"] = period
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    df = df.dropna(subset=["code", "datetime", "open", "high", "low", "close"]).copy()
    df["code"] = df["code"].astype(str).str.upper()
    df["open"] = pd.to_numeric(df["open"], errors="coerce")
    df["high"] = pd.to_numeric(df["high"], errors="coerce")
    df["low"] = pd.to_numeric(df["low"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0)
    df = df.dropna(subset=["open", "high", "low", "close"]).copy()
    df["created_at"] = created_at
    df["id"] = [stable_id(period, code, dt) for code, dt in zip(df["code"], df["datetime"])]
    return df[STAGE_COLUMNS]


def insert_stage(args: argparse.Namespace, client) -> dict[str, Any]:
    root = Path(args.warehouse_root)
    periods = parse_periods(args.periods)
    created_at = datetime.now().replace(microsecond=0)
    ensure_stage_table(client, args.stage_table, reset=args.reset_stage)
    summary: dict[str, Any] = {"phase": "insert_stage", "periods": {}, "created_at": created_at.isoformat()}
    for period in periods:
        files = period_files(root, period)
        if args.limit_files > 0:
            files = files[: args.limit_files]
        rows = 0
        started = time.perf_counter()
        for idx, path in enumerate(files, start=1):
            frame = load_parquet_for_insert(path, period, created_at)
            if frame.empty:
                continue
            rows += len(frame)
            if not args.dry_run:
                client.insert(args.stage_table, [tuple(row) for row in frame.itertuples(index=False, name=None)], column_names=STAGE_COLUMNS)
            if idx % args.progress_every == 0 or idx == len(files):
                log(f"stage period={period} files={idx}/{len(files)} rows={rows:,}")
        summary["periods"][period] = {
            "files": len(files),
            "rows": rows,
            "elapsed_sec": round(time.perf_counter() - started, 3),
        }
    return summary


def apply_missing(args: argparse.Namespace, client) -> dict[str, Any]:
    periods = parse_periods(args.periods)
    summary: dict[str, Any] = {"phase": "apply_missing", "periods": {}}
    for period in periods:
        target = PERIOD_TO_TABLE[period]
        months = client.query(
            f"""
            SELECT DISTINCT toYYYYMM(datetime) AS ym
            FROM {args.stage_table}
            WHERE period = {quote_sql(period)}
            ORDER BY ym
            """
        ).result_rows
        inserted_total = 0
        started = time.perf_counter()
        for (ym,) in months:
            before = client.query(
                f"""
                SELECT count()
                FROM {target}
                WHERE toYYYYMM(datetime) = {int(ym)}
                """
            ).first_row[0]
            if not args.dry_run:
                client.command(
                    f"""
                    INSERT INTO {target} ({", ".join(TARGET_COLUMNS)})
                    SELECT
                        s.code,
                        s.datetime,
                        s.open,
                        s.high,
                        s.low,
                        s.close,
                        {'toInt64(round(s.volume))' if period == '5m' else 's.volume'} AS volume,
                        s.amount,
                        s.created_at,
                        s.id
                    FROM
                    (
                        SELECT period, code, datetime, open, high, low, close, volume, amount, created_at, id
                        FROM {args.stage_table}
                        WHERE period = {quote_sql(period)}
                          AND toYYYYMM(datetime) = {int(ym)}
                    ) AS s
                    ANY LEFT JOIN
                    (
                        SELECT code, datetime
                        FROM {target}
                        WHERE toYYYYMM(datetime) = {int(ym)}
                    ) AS t
                    ON s.code = t.code AND s.datetime = t.datetime
                    WHERE t.datetime IS NULL
                    SETTINGS max_threads = 2, join_algorithm = 'grace_hash'
                    """
                )
            after = client.query(
                f"""
                SELECT count()
                FROM {target}
                WHERE toYYYYMM(datetime) = {int(ym)}
                """
            ).first_row[0]
            inserted = int(after or 0) - int(before or 0)
            inserted_total += inserted
            log(f"apply period={period} ym={ym} inserted_missing={inserted:,}")
        summary["periods"][period] = {
            "months": len(months),
            "inserted_missing_rows": inserted_total,
            "elapsed_sec": round(time.perf_counter() - started, 3),
        }
    return summary


def validate(args: argparse.Namespace, client) -> dict[str, Any]:
    periods = parse_periods(args.periods)
    result: dict[str, Any] = {"phase": "validate", "periods": {}}
    for period in periods:
        target = PERIOD_TO_TABLE[period]
        stage = client.query(
            f"""
            SELECT count(), uniqExact(code), min(datetime), max(datetime)
            FROM {args.stage_table}
            WHERE period = {quote_sql(period)}
            """
        ).first_row
        target_rows = client.query(
            f"""
            SELECT count(), uniqExact(code), min(datetime), max(datetime)
            FROM {target}
            WHERE datetime >= toDateTime({quote_sql(args.start_date + ' 00:00:00')})
              AND datetime < toDateTime({quote_sql(args.end_next_date + ' 00:00:00')})
            """
        ).first_row
        months = client.query(
            f"""
            SELECT DISTINCT toYYYYMM(datetime) AS ym
            FROM {args.stage_table}
            WHERE period = {quote_sql(period)}
            ORDER BY ym
            """
        ).result_rows
        missing = 0
        for (ym,) in months:
            month_missing = client.query(
                f"""
                SELECT count()
                FROM
                (
                    SELECT code, datetime
                    FROM {args.stage_table}
                    WHERE period = {quote_sql(period)}
                      AND toYYYYMM(datetime) = {int(ym)}
                ) AS s
                ANY LEFT JOIN
                (
                    SELECT code, datetime
                    FROM {target}
                    WHERE toYYYYMM(datetime) = {int(ym)}
                ) AS t
                ON s.code = t.code AND s.datetime = t.datetime
                WHERE t.datetime IS NULL
                SETTINGS max_threads = 2, join_algorithm = 'grace_hash'
                """
            ).first_row[0]
            missing += int(month_missing or 0)
        result["periods"][period] = {
            "stage": row_tuple(stage),
            "target_window": row_tuple(target_rows),
            "stage_keys_missing_in_target": int(missing or 0),
        }
    return result


def row_tuple(row: Any) -> dict[str, Any]:
    return {
        "rows": int(row[0] or 0),
        "codes": int(row[1] or 0),
        "min_datetime": str(row[2]) if row[2] is not None else None,
        "max_datetime": str(row[3]) if row[3] is not None else None,
    }


def parse_periods(text: str) -> list[str]:
    periods = [item.strip().lower() for item in text.split(",") if item.strip()]
    unknown = sorted(set(periods) - set(PERIOD_TO_TABLE))
    if unknown:
        raise ValueError(f"unsupported periods: {unknown}")
    return periods


def write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Import parsed QMT datadir minute parquet warehouse into ClickHouse.")
    parser.add_argument("--warehouse-root", default=str(DEFAULT_WAREHOUSE_ROOT))
    parser.add_argument("--stage-table", default=DEFAULT_STAGE_TABLE)
    parser.add_argument("--periods", default="5m,15m,30m,60m")
    parser.add_argument("--phase", choices=["insert-stage", "apply-missing", "validate", "all"], default="all")
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    parser.add_argument("--start-date", default="2025-07-04")
    parser.add_argument("--end-next-date", default="2026-07-08")
    parser.add_argument("--reset-stage", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit-files", type=int, default=0)
    parser.add_argument("--progress-every", type=int, default=50)
    args = parser.parse_args()

    client = ch_client()
    report: dict[str, Any] = {
        "warehouse_root": args.warehouse_root,
        "stage_table": args.stage_table,
        "periods": parse_periods(args.periods),
        "dry_run": args.dry_run,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "steps": [],
    }
    if args.phase in {"insert-stage", "all"}:
        report["steps"].append(insert_stage(args, client))
    if args.phase in {"apply-missing", "all"}:
        report["steps"].append(apply_missing(args, client))
    if args.phase in {"validate", "all"}:
        report["steps"].append(validate(args, client))
    report["finished_at"] = datetime.now().isoformat(timespec="seconds")
    write_report(Path(args.report), report)
    log(f"完成：report={args.report}")


if __name__ == "__main__":
    main()

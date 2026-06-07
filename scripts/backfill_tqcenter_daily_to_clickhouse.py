from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import pandas as pd
from clickhouse_connect import get_client


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STAGE_TABLE = "kline_daily_tqcenter_2010_stage"
COLUMNS = [
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
]


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def quote_sql(value: Any) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def chunked(items: list[str], size: int) -> Iterable[list[str]]:
    for idx in range(0, len(items), size):
        yield items[idx : idx + size]


def ch_client():
    return get_client(host="127.0.0.1", port=8123, username="default", database="stock")


def ensure_tq():
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from data_fetcher.sources.tdxquant_pool import tdxquant_pool

    return tdxquant_pool.get_client()


def ensure_stage_table(client, stage_table: str) -> None:
    client.command(
        f"""
        CREATE TABLE IF NOT EXISTS {stage_table}
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
            id UInt64 DEFAULT 0
        )
        ENGINE = ReplacingMergeTree
        ORDER BY (code, trade_date)
        """
    )


def load_codes(client, codes_arg: str, limit: int) -> list[str]:
    if codes_arg.strip():
        codes = [item.strip().upper() for item in codes_arg.split(",") if item.strip()]
    else:
        rows = client.query(
            """
            SELECT code
            FROM stocks
            WHERE type IN ('stock', 'index')
            ORDER BY type, code
            """
        ).result_rows
        codes = [row[0] for row in rows]
    if limit > 0:
        codes = codes[:limit]
    return codes


def existing_stage_codes(client, stage_table: str) -> set[str]:
    try:
        rows = client.query(f"SELECT DISTINCT code FROM {stage_table}").result_rows
    except Exception:
        return set()
    return {row[0] for row in rows}


def frame_for(data: dict[str, Any], field: str) -> pd.DataFrame:
    value = data.get(field)
    if isinstance(value, pd.DataFrame):
        return value.copy()
    return pd.DataFrame()


def rows_from_market_data(data: dict[str, Any], batch_codes: list[str], created_at: datetime) -> tuple[list[tuple], dict[str, int]]:
    close_df = frame_for(data, "Close")
    if close_df.empty:
        return [], {code: 0 for code in batch_codes}

    frames = {
        "open": frame_for(data, "Open"),
        "high": frame_for(data, "High"),
        "low": frame_for(data, "Low"),
        "close": close_df,
        "volume": frame_for(data, "Volume"),
        "amount": frame_for(data, "Amount"),
    }
    rows: list[tuple] = []
    counts: dict[str, int] = {}

    for code in batch_codes:
        if code not in close_df.columns:
            counts[code] = 0
            continue
        one = pd.DataFrame(index=close_df.index)
        missing_core = False
        for name, frame in frames.items():
            if frame.empty or code not in frame.columns:
                if name in {"open", "high", "low", "close"}:
                    missing_core = True
                    break
                one[name] = 0.0
            else:
                one[name] = pd.to_numeric(frame[code], errors="coerce")
        if missing_core:
            counts[code] = 0
            continue
        one = one.dropna(subset=["open", "high", "low", "close"]).copy()
        if one.empty:
            counts[code] = 0
            continue
        one.index = pd.to_datetime(one.index, errors="coerce")
        one = one[~one.index.isna()].sort_index()
        one = one[~one.index.duplicated(keep="last")]
        prev_close = one["close"].shift(1)
        change_amount = (one["close"] - prev_close).fillna(0.0)
        change_pct = ((change_amount / prev_close.replace(0, pd.NA)) * 100).fillna(0.0)
        amplitude = (((one["high"] - one["low"]) / prev_close.replace(0, pd.NA)) * 100).fillna(0.0)
        count = 0
        for idx, values in one.iterrows():
            rows.append(
                (
                    code,
                    idx.date(),
                    float(values["open"] or 0),
                    float(values["high"] or 0),
                    float(values["low"] or 0),
                    float(values["close"] or 0),
                    float(values.get("volume", 0) or 0),
                    float(values.get("amount", 0) or 0),
                    float(amplitude.loc[idx] or 0),
                    float(change_pct.loc[idx] or 0),
                    float(change_amount.loc[idx] or 0),
                    0.0,
                    created_at,
                )
            )
            count += 1
        counts[code] = count
    return rows, counts


def fetch_to_stage(args: argparse.Namespace) -> dict[str, Any]:
    client = ch_client()
    ensure_stage_table(client, args.stage_table)
    if args.reset_stage:
        log(f"truncate stage table {args.stage_table}")
        client.command(f"TRUNCATE TABLE {args.stage_table}")

    codes = load_codes(client, args.codes, args.limit)
    done_codes = existing_stage_codes(client, args.stage_table) if args.resume else set()
    target_codes = [code for code in codes if code not in done_codes]
    log(f"fetch target codes={len(target_codes)} skipped_existing={len(done_codes)} batch_size={args.batch_size}")

    tq = ensure_tq()
    created_at = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    total_rows = 0
    empty_codes: list[str] = []
    failed: list[dict[str, str]] = []
    started = time.perf_counter()

    for batch_no, batch_codes in enumerate(chunked(target_codes, args.batch_size), start=1):
        batch_started = time.perf_counter()
        try:
            data = tq.get_market_data(
                field_list=[],
                stock_list=batch_codes,
                period="1d",
                start_time=args.start_date.replace("-", ""),
                end_time=args.end_date.replace("-", ""),
                count=-1,
                dividend_type=args.dividend_type,
                fill_data=False,
            )
            if not isinstance(data, dict) or not data:
                empty_codes.extend(batch_codes)
                log(f"batch {batch_no}: empty response codes={len(batch_codes)}")
                continue
            rows, counts = rows_from_market_data(data, batch_codes, created_at)
            empty_codes.extend([code for code, count in counts.items() if count == 0])
            if rows:
                client.insert(args.stage_table, rows, column_names=COLUMNS)
                total_rows += len(rows)
            log(
                f"batch {batch_no}: codes={len(batch_codes)} rows={len(rows)} "
                f"elapsed={time.perf_counter() - batch_started:.2f}s total_rows={total_rows}"
            )
        except Exception as exc:
            failed.append({"batch": str(batch_no), "codes": ",".join(batch_codes), "error": f"{type(exc).__name__}: {exc}"})
            log(f"batch {batch_no}: failed {type(exc).__name__}: {exc}")
        if args.sleep > 0:
            time.sleep(args.sleep)

    summary = {
        "phase": "fetch",
        "codes": len(codes),
        "target_codes": len(target_codes),
        "stage_rows_inserted_this_run": total_rows,
        "empty_codes": len(set(empty_codes)),
        "failed_batches": len(failed),
        "elapsed_sec": round(time.perf_counter() - started, 3),
    }
    report = {"summary": summary, "empty_codes_sample": sorted(set(empty_codes))[:200], "failed": failed}
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"fetch summary {json.dumps(summary, ensure_ascii=False)} report={args.report}")
    return summary


def validate_stage(args: argparse.Namespace) -> dict[str, Any]:
    client = ch_client()
    codes = load_codes(client, args.codes, args.limit)
    code_sql = ",".join(quote_sql(code) for code in codes)
    result = client.query(
        f"""
        SELECT
            count() AS rows,
            uniqExact(code) AS codes,
            min(trade_date) AS min_date,
            max(trade_date) AS max_date
        FROM {args.stage_table}
        WHERE code IN ({code_sql})
        """
    ).first_row
    missing = client.query(
        f"""
        SELECT s.code, s.name, s.type, s.list_date
        FROM stocks s
        LEFT JOIN (SELECT DISTINCT code FROM {args.stage_table}) k ON s.code = k.code
        WHERE s.code IN ({code_sql})
          AND (k.code = '' OR k.code IS NULL)
        ORDER BY s.type, s.code
        LIMIT 200
        """
    ).result_rows
    summary = {
        "phase": "validate_stage",
        "target_codes": len(codes),
        "stage_rows": int(result[0] or 0),
        "stage_codes": int(result[1] or 0),
        "min_date": str(result[2]) if result[2] else None,
        "max_date": str(result[3]) if result[3] else None,
        "missing_codes_sample": [row[0] for row in missing],
        "missing_codes_sample_count": len(missing),
    }
    log(f"stage validation {json.dumps(summary, ensure_ascii=False)}")
    return summary


def apply_stage(args: argparse.Namespace) -> dict[str, Any]:
    client = ch_client()
    codes = load_codes(client, args.codes, args.limit)
    total_inserted = 0
    started = time.perf_counter()
    log(f"apply stage to kline_daily codes={len(codes)} chunk_size={args.delete_chunk_size}")
    for chunk_no, code_chunk in enumerate(chunked(codes, args.delete_chunk_size), start=1):
        code_sql = ",".join(quote_sql(code) for code in code_chunk)
        log(f"apply chunk {chunk_no}: deleting target rows codes={len(code_chunk)}")
        client.command(
            f"""
            ALTER TABLE kline_daily
            DELETE WHERE trade_date >= toDate({quote_sql(args.start_date)})
              AND code IN ({code_sql})
            SETTINGS mutations_sync = 1
            """
        )
        log(f"apply chunk {chunk_no}: inserting from stage")
        client.command(
            f"""
            INSERT INTO kline_daily ({", ".join(COLUMNS)})
            SELECT {", ".join(COLUMNS)}
            FROM {args.stage_table}
            WHERE code IN ({code_sql})
            """
        )
        inserted = client.query(
            f"SELECT count() FROM {args.stage_table} WHERE code IN ({code_sql})"
        ).first_row[0]
        total_inserted += int(inserted or 0)
        log(f"apply chunk {chunk_no}: inserted={inserted} total_inserted={total_inserted}")
    summary = {
        "phase": "apply",
        "codes": len(codes),
        "inserted_rows": total_inserted,
        "elapsed_sec": round(time.perf_counter() - started, 3),
    }
    log(f"apply summary {json.dumps(summary, ensure_ascii=False)}")
    return summary


def validate_target(args: argparse.Namespace) -> dict[str, Any]:
    client = ch_client()
    codes = load_codes(client, args.codes, args.limit)
    code_sql = ",".join(quote_sql(code) for code in codes)
    overall = client.query(
        f"""
        SELECT count() rows, uniqExact(code) codes, min(trade_date) min_date, max(trade_date) max_date
        FROM kline_daily
        WHERE code IN ({code_sql}) AND trade_date >= toDate({quote_sql(args.start_date)})
        """
    ).first_row
    duplicates = client.query(
        f"""
        SELECT count()
        FROM (
            SELECT code, trade_date, count() c
            FROM kline_daily
            WHERE code IN ({code_sql}) AND trade_date >= toDate({quote_sql(args.start_date)})
            GROUP BY code, trade_date
            HAVING c > 1
        )
        """
    ).first_row[0]
    laggards = client.query(
        f"""
        SELECT s.code, s.name, s.type, min(k.trade_date) first_date, max(k.trade_date) last_date, count() rows
        FROM stocks s
        LEFT JOIN kline_daily k ON s.code = k.code AND k.trade_date >= toDate({quote_sql(args.start_date)})
        WHERE s.code IN ({code_sql})
        GROUP BY s.code, s.name, s.type
        HAVING rows = 0 OR last_date < (SELECT max(trade_date) FROM kline_daily)
        ORDER BY rows ASC, last_date ASC, s.type, s.code
        LIMIT 200
        """
    ).result_rows
    summary = {
        "phase": "validate_target",
        "target_codes": len(codes),
        "rows": int(overall[0] or 0),
        "codes": int(overall[1] or 0),
        "min_date": str(overall[2]) if overall[2] else None,
        "max_date": str(overall[3]) if overall[3] else None,
        "duplicate_keys": int(duplicates or 0),
        "laggards_sample": [
            {
                "code": row[0],
                "name": row[1],
                "type": row[2],
                "first_date": str(row[3]) if row[3] else None,
                "last_date": str(row[4]) if row[4] else None,
                "rows": int(row[5] or 0),
            }
            for row in laggards
        ],
    }
    log(f"target validation {json.dumps(summary, ensure_ascii=False)}")
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill daily bars from local tqcenter.py into ClickHouse.")
    parser.add_argument("--phase", choices=["fetch", "validate-stage", "apply", "validate-target", "all"], default="all")
    parser.add_argument("--start-date", default="2010-01-01")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--codes", default="", help="Comma-separated code list. Empty means all stocks and indexes.")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=80)
    parser.add_argument("--delete-chunk-size", type=int, default=250)
    parser.add_argument("--stage-table", default=DEFAULT_STAGE_TABLE)
    parser.add_argument("--reset-stage", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--sleep", type=float, default=0.02)
    parser.add_argument("--dividend-type", default="none", choices=["none", "front", "back"])
    parser.add_argument("--report", default=str(REPO_ROOT / "reports" / "tqcenter_daily_backfill_report.json"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    summaries: list[dict[str, Any]] = []
    if args.phase in {"fetch", "all"}:
        summaries.append(fetch_to_stage(args))
    if args.phase in {"validate-stage", "all"}:
        summaries.append(validate_stage(args))
    if args.phase in {"apply", "all"}:
        summaries.append(apply_stage(args))
    if args.phase in {"validate-target", "all"}:
        summaries.append(validate_target(args))
    log("done " + json.dumps(summaries, ensure_ascii=False))


if __name__ == "__main__":
    main()

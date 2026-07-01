from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import pandas as pd
from clickhouse_connect import get_client


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from data_fetcher.sources.qmtmini_client import QmtMiniMarketClient
from utils.paths import report_path


DEFAULT_STAGE_TABLE = "kline_daily_qmtmini_stage"
FIELD_LIST = ["open", "high", "low", "close", "volume", "amount"]
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
]
QMT_DAILY_CODE_ALIASES = {
    # Strategy code kept by AiStock/TDX history; QMT Mini exposes SSE Composite as 000001.SH.
    "999999.SH": "000001.SH",
}


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def quote_sql(value: Any) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def chunked(items: list[str], size: int) -> Iterable[list[str]]:
    for idx in range(0, len(items), size):
        yield items[idx : idx + size]


def qmt_source_code(code: str) -> str:
    return QMT_DAILY_CODE_ALIASES.get(str(code).upper(), str(code).upper())


def ch_client():
    return get_client(
        host=os.getenv("AISTOCK_CLICKHOUSE_HOST", "127.0.0.1"),
        port=int(os.getenv("AISTOCK_CLICKHOUSE_PORT", "8123")),
        username=os.getenv("AISTOCK_CLICKHOUSE_USER", "default"),
        password=os.getenv("AISTOCK_CLICKHOUSE_PASSWORD", ""),
        database=os.getenv("AISTOCK_CLICKHOUSE_DATABASE", "stock"),
    )


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


def ensure_request_log_table(client) -> None:
    client.command(
        """
        CREATE TABLE IF NOT EXISTS qmt_xtquant_request_log
        (
            request_id String,
            task_name String,
            phase String,
            period String,
            code_count UInt32,
            start_time String,
            end_time String,
            status String,
            attempts UInt8,
            elapsed_sec Float64,
            rows_returned UInt64,
            error String,
            created_at DateTime
        )
        ENGINE = MergeTree
        ORDER BY (created_at, task_name, phase, period)
        """
    )


def insert_request_log(
    client,
    *,
    task_name: str,
    phase: str,
    period: str,
    codes: list[str],
    start_time: str,
    end_time: str,
    status: str,
    attempts: int,
    elapsed_sec: float,
    rows_returned: int,
    error: str = "",
) -> None:
    try:
        ensure_request_log_table(client)
        now = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
        request_id = f"{task_name}:{phase}:{period}:{now:%Y%m%d%H%M%S%f}:{len(codes)}"
        client.insert(
            "qmt_xtquant_request_log",
            [
                (
                    request_id,
                    task_name,
                    phase,
                    period,
                    len(codes),
                    start_time,
                    end_time,
                    status,
                    int(attempts),
                    float(round(elapsed_sec, 6)),
                    int(rows_returned),
                    str(error or "")[:2000],
                    now,
                )
            ],
            column_names=[
                "request_id",
                "task_name",
                "phase",
                "period",
                "code_count",
                "start_time",
                "end_time",
                "status",
                "attempts",
                "elapsed_sec",
                "rows_returned",
                "error",
                "created_at",
            ],
        )
    except Exception as exc:
        log(f"request log insert skipped: {type(exc).__name__}: {exc}")


def load_codes(client, codes_arg: str, limit: int, include_index: bool, end_date: str = "") -> list[str]:
    if codes_arg.strip():
        codes = [item.strip().upper() for item in codes_arg.split(",") if item.strip()]
    else:
        type_filter = "type IN ('stock', 'index')" if include_index else "type = 'stock'"
        list_date_filter = ""
        if end_date.strip():
            list_date_filter = f"AND (type = 'index' OR list_date IS NULL OR list_date <= toDate({quote_sql(end_date)}))"
        rows = client.query(
            f"""
            SELECT code
            FROM stocks
            WHERE {type_filter}
              {list_date_filter}
            ORDER BY type, code
            """
        ).result_rows
        codes = [str(row[0]).upper() for row in rows]
    return codes[:limit] if limit > 0 else codes


def existing_stage_codes(client, stage_table: str) -> set[str]:
    try:
        rows = client.query(f"SELECT DISTINCT code FROM {stage_table}").result_rows
    except Exception:
        return set()
    return {str(row[0]).upper() for row in rows}


def _field_frame(data: dict[str, Any], field: str) -> pd.DataFrame:
    value = data.get(field)
    if value is None:
        value = data.get(field.capitalize())
    return value.copy() if isinstance(value, pd.DataFrame) else pd.DataFrame()


def _normalize_date(value: Any) -> pd.Timestamp | None:
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        text = str(value)
        if text.endswith(".0"):
            text = text[:-2]
        if len(text) >= 8:
            parsed = pd.to_datetime(text[:8], format="%Y%m%d", errors="coerce")
    if pd.isna(parsed):
        return None
    return pd.Timestamp(parsed).normalize()


def rows_from_qmt(
    data: dict[str, Any],
    batch_codes: list[str],
    created_at: datetime,
    start_date: str,
    end_date: str,
) -> tuple[list[tuple], dict[str, int]]:
    close_df = _field_frame(data, "close")
    if close_df.empty:
        return [], {code: 0 for code in batch_codes}

    frames = {field: _field_frame(data, field) for field in FIELD_LIST}
    rows: list[tuple] = []
    counts: dict[str, int] = {}
    for code in batch_codes:
        source_code = qmt_source_code(code)
        if source_code in close_df.index:
            one = pd.DataFrame(index=close_df.columns)
            for field, frame in frames.items():
                one[field] = pd.to_numeric(frame.loc[source_code], errors="coerce") if source_code in frame.index else 0.0
        elif source_code in close_df.columns:
            one = pd.DataFrame(index=close_df.index)
            for field, frame in frames.items():
                one[field] = pd.to_numeric(frame[source_code], errors="coerce") if source_code in frame.columns else 0.0
        else:
            counts[code] = 0
            continue

        normalized_index = [_normalize_date(item) for item in one.index]
        one.index = normalized_index
        one = one[~pd.isna(one.index)].copy()
        one = one.dropna(subset=["open", "high", "low", "close"]).sort_index()
        one = one[~one.index.duplicated(keep="last")]
        start_ts = pd.Timestamp(start_date).normalize()
        end_ts = pd.Timestamp(end_date).normalize()
        one = one[(one.index >= start_ts) & (one.index <= end_ts)]
        if one.empty:
            counts[code] = 0
            continue

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


def write_report(path: str, payload: dict[str, Any]) -> None:
    report = Path(path)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def fetch_to_stage(args: argparse.Namespace) -> dict[str, Any]:
    client = ch_client()
    ensure_stage_table(client, args.stage_table)
    ensure_request_log_table(client)
    if args.reset_stage:
        log(f"truncate stage table {args.stage_table}")
        client.command(f"TRUNCATE TABLE {args.stage_table}")

    codes = load_codes(client, args.codes, args.limit, args.include_index, args.end_date)
    done_codes = existing_stage_codes(client, args.stage_table) if args.resume else set()
    target_codes = [code for code in codes if code not in done_codes]
    market = QmtMiniMarketClient()
    market.connect()
    created_at = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    total_rows = 0
    empty_codes: list[str] = []
    failed: list[dict[str, str]] = []
    started = time.perf_counter()
    log(f"qmt fetch target_codes={len(target_codes)} skipped_existing={len(done_codes)} batch_size={args.batch_size}")

    for batch_no, batch_codes in enumerate(chunked(target_codes, args.batch_size), start=1):
        batch_started = time.perf_counter()
        attempts = 0
        batch_error = ""
        rows: list[tuple] = []
        qmt_batch_codes = sorted({qmt_source_code(code) for code in batch_codes})
        try:
            for attempt in range(1, args.max_retries + 2):
                attempts = attempt
                try:
                    if args.use_batch_download:
                        market.download_history_data2(
                            qmt_batch_codes,
                            "1d",
                            args.start_date.replace("-", ""),
                            args.end_date.replace("-", ""),
                        )
                    else:
                        for code in qmt_batch_codes:
                            market.download_history_data(code, "1d", args.start_date.replace("-", ""), args.end_date.replace("-", ""))
                    batch_error = ""
                    break
                except Exception as exc:
                    batch_error = f"{type(exc).__name__}: {exc}"
                    if attempt > args.max_retries:
                        raise
                    time.sleep(args.retry_sleep)
            data = market.get_market_data(
                field_list=FIELD_LIST,
                stock_list=qmt_batch_codes,
                period="1d",
                start_time=args.start_date.replace("-", ""),
                end_time=args.end_date.replace("-", ""),
                count=-1,
                dividend_type=args.dividend_type,
                fill_data=False,
            )
            rows, counts = rows_from_qmt(data, batch_codes, created_at, args.start_date, args.end_date)
            empty_codes.extend([code for code, count in counts.items() if count == 0])
            if rows:
                client.insert(args.stage_table, rows, column_names=TARGET_COLUMNS)
                total_rows += len(rows)
            elapsed = time.perf_counter() - batch_started
            insert_request_log(
                client,
                task_name="qmt_xtquant_daily_backfill",
                phase="fetch",
                period="1d",
                codes=batch_codes,
                start_time=args.start_date,
                end_time=args.end_date,
                status="success",
                attempts=attempts,
                elapsed_sec=elapsed,
                rows_returned=len(rows),
            )
            log(f"batch {batch_no}: codes={len(batch_codes)} rows={len(rows)} attempts={attempts} elapsed={elapsed:.2f}s")
        except Exception as exc:
            elapsed = time.perf_counter() - batch_started
            failed.append({"batch": str(batch_no), "codes": ",".join(batch_codes), "error": f"{type(exc).__name__}: {exc}"})
            insert_request_log(
                client,
                task_name="qmt_xtquant_daily_backfill",
                phase="fetch",
                period="1d",
                codes=batch_codes,
                start_time=args.start_date,
                end_time=args.end_date,
                status="failed",
                attempts=attempts or 1,
                elapsed_sec=elapsed,
                rows_returned=len(rows),
                error=batch_error or f"{type(exc).__name__}: {exc}",
            )
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
    write_report(args.report, {"summary": summary, "empty_codes_sample": sorted(set(empty_codes))[:200], "failed": failed})
    if failed:
        raise RuntimeError(f"qmt daily fetch has failed batches: {len(failed)} report={args.report}")
    return summary


def validate_stage(args: argparse.Namespace) -> dict[str, Any]:
    client = ch_client()
    codes = load_codes(client, args.codes, args.limit, args.include_index, args.end_date)
    code_sql = ",".join(quote_sql(code) for code in codes) or "''"
    summary_row = client.query(
        f"""
        SELECT count(), uniqExact(code), min(trade_date), max(trade_date)
        FROM {args.stage_table}
        WHERE code IN ({code_sql})
        """
    ).first_row
    compare_rows = client.query(
        f"""
        SELECT
            count() AS overlap_rows,
            countIf(abs(q.close - k.close) > {args.price_tolerance}) AS close_diff_rows,
            max(abs(q.close - k.close)) AS max_close_abs_diff,
            avg(abs(q.close - k.close)) AS avg_close_abs_diff,
            countIf(abs(q.volume - k.volume) > {args.volume_tolerance}) AS volume_diff_rows
        FROM {args.stage_table} q
        INNER JOIN (SELECT * FROM kline_daily FINAL) k ON q.code = k.code AND q.trade_date = k.trade_date
        WHERE q.code IN ({code_sql})
          AND q.trade_date >= toDate({quote_sql(args.start_date)})
          AND q.trade_date <= toDate({quote_sql(args.end_date)})
        """
    ).first_row
    date_rows = client.query(
        f"""
        SELECT
            q.trade_date,
            uniqExact(q.code) AS qmt_codes,
            uniqExact(k.code) AS current_codes,
            countIf(k.code = '' OR k.code IS NULL) AS qmt_only_rows
        FROM {args.stage_table} q
        LEFT JOIN (SELECT * FROM kline_daily FINAL) k ON q.code = k.code AND q.trade_date = k.trade_date
        WHERE q.code IN ({code_sql})
          AND q.trade_date >= toDate({quote_sql(args.start_date)})
          AND q.trade_date <= toDate({quote_sql(args.end_date)})
        GROUP BY q.trade_date
        ORDER BY q.trade_date DESC
        LIMIT 20
        """
    ).result_rows
    missing_rows = client.query(
        f"""
        SELECT s.code, s.name, s.type
        FROM stocks s
        LEFT JOIN (SELECT DISTINCT code FROM {args.stage_table}) q ON s.code = q.code
        WHERE s.code IN ({code_sql}) AND (q.code = '' OR q.code IS NULL)
        ORDER BY s.type, s.code
        LIMIT 100
        """
    ).result_rows
    summary = {
        "phase": "validate_stage",
        "target_codes": len(codes),
        "stage_rows": int(summary_row[0] or 0),
        "stage_codes": int(summary_row[1] or 0),
        "min_date": str(summary_row[2]) if summary_row[2] else None,
        "max_date": str(summary_row[3]) if summary_row[3] else None,
        "missing_codes_sample": [{"code": row[0], "name": row[1], "type": row[2]} for row in missing_rows],
        "overlap_rows": int(compare_rows[0] or 0),
        "close_diff_rows": int(compare_rows[1] or 0),
        "max_close_abs_diff": float(compare_rows[2] or 0),
        "avg_close_abs_diff": float(compare_rows[3] or 0),
        "volume_diff_rows": int(compare_rows[4] or 0),
        "latest_date_coverage": [
            {
                "trade_date": str(row[0]),
                "qmt_codes": int(row[1] or 0),
                "current_codes": int(row[2] or 0),
                "qmt_only_rows": int(row[3] or 0),
            }
            for row in date_rows
        ],
    }
    write_report(args.report, {"summary": summary})
    log("stage validation " + json.dumps(summary, ensure_ascii=False))
    return summary


def apply_stage(args: argparse.Namespace) -> dict[str, Any]:
    client = ch_client()
    codes = load_codes(client, args.codes, args.limit, args.include_index, args.end_date)
    total_inserted = 0
    for chunk_no, code_chunk in enumerate(chunked(codes, args.delete_chunk_size), start=1):
        code_sql = ",".join(quote_sql(code) for code in code_chunk)
        log(f"apply chunk {chunk_no}: delete+insert codes={len(code_chunk)}")
        client.command(
            f"""
            ALTER TABLE kline_daily
            DELETE WHERE trade_date >= toDate({quote_sql(args.start_date)})
              AND trade_date <= toDate({quote_sql(args.end_date)})
              AND code IN ({code_sql})
            SETTINGS mutations_sync = 1
            """
        )
        client.command(
            f"""
            INSERT INTO kline_daily ({", ".join(TARGET_COLUMNS)})
            SELECT {", ".join(TARGET_COLUMNS)}
            FROM {args.stage_table}
            WHERE code IN ({code_sql})
              AND trade_date >= toDate({quote_sql(args.start_date)})
              AND trade_date <= toDate({quote_sql(args.end_date)})
            """
        )
        inserted = client.query(
            f"""
            SELECT count()
            FROM {args.stage_table}
            WHERE code IN ({code_sql})
              AND trade_date >= toDate({quote_sql(args.start_date)})
              AND trade_date <= toDate({quote_sql(args.end_date)})
            """
        ).first_row[0]
        total_inserted += int(inserted or 0)
    summary = {"phase": "apply", "codes": len(codes), "inserted_rows": total_inserted}
    write_report(args.report, {"summary": summary})
    log("apply summary " + json.dumps(summary, ensure_ascii=False))
    return summary


def validate_target(args: argparse.Namespace) -> dict[str, Any]:
    client = ch_client()
    codes = load_codes(client, args.codes, args.limit, args.include_index, args.end_date)
    code_sql = ",".join(quote_sql(code) for code in codes) or "''"
    row = client.query(
        f"""
        SELECT count(), uniqExact(code), min(trade_date), max(trade_date)
        FROM kline_daily
        WHERE code IN ({code_sql})
          AND trade_date >= toDate({quote_sql(args.start_date)})
          AND trade_date <= toDate({quote_sql(args.end_date)})
        """
    ).first_row
    dup = client.query(
        f"""
        SELECT count()
        FROM (
            SELECT code, trade_date, count() c
            FROM kline_daily
            WHERE code IN ({code_sql})
              AND trade_date >= toDate({quote_sql(args.start_date)})
              AND trade_date <= toDate({quote_sql(args.end_date)})
            GROUP BY code, trade_date
            HAVING c > 1
        )
        """
    ).first_row[0]
    summary = {
        "phase": "validate_target",
        "target_codes": len(codes),
        "rows": int(row[0] or 0),
        "codes": int(row[1] or 0),
        "min_date": str(row[2]) if row[2] else None,
        "max_date": str(row[3]) if row[3] else None,
        "duplicate_keys": int(dup or 0),
    }
    write_report(args.report, {"summary": summary})
    log("target validation " + json.dumps(summary, ensure_ascii=False))
    return summary


def parse_args() -> argparse.Namespace:
    today = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    default_report = report_path("qmtmini_daily_backfill_validate", f"qmtmini_daily_{today.replace('-', '')}.json")
    parser = argparse.ArgumentParser(description="Fetch and validate QMT Mini 1d bars against ClickHouse kline_daily.")
    parser.add_argument("--phase", choices=["fetch", "validate-stage", "apply", "validate-target", "all"], default="validate-stage")
    parser.add_argument("--start-date", default="2026-06-01")
    parser.add_argument("--end-date", default=today)
    parser.add_argument("--codes", default="", help="Comma-separated stock codes. Empty means stocks from ClickHouse.")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--include-index", action="store_true")
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--delete-chunk-size", type=int, default=200)
    parser.add_argument("--stage-table", default=DEFAULT_STAGE_TABLE)
    parser.add_argument("--reset-stage", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--sleep", type=float, default=0.02)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--retry-sleep", type=float, default=1.0)
    parser.add_argument("--use-batch-download", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--dividend-type", default="none", choices=["none", "front", "back"])
    parser.add_argument("--price-tolerance", type=float, default=0.001)
    parser.add_argument("--volume-tolerance", type=float, default=1.0)
    parser.add_argument("--report", default=str(default_report))
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

"""
Retry exact BaoStock minute chunks currently marked failed in ClickHouse.

This is intentionally narrower than collect_baostock_all_minutes.py: it reads
failed state rows and retries only those code/month chunks, then derives the
higher minute periods from the repaired 5m data.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts.collect_baostock_all_minutes import (  # noqa: E402
    FREQ_TABLE_MAP,
    STATE_TABLE,
    _upsert_to_ch,
    derive_higher_frame,
    ensure_meta_tables,
    fetch_one_with_retries,
    filter_missing_rows,
    load_chunk,
    mark_state,
)
from utils.market_warehouse import clickhouse_client  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Retry exact failed BaoStock minute chunks.")
    parser.add_argument("--start-date", default="2020-01-01")
    parser.add_argument("--end-date", default="2023-12-31")
    parser.add_argument("--source-tag", default="baostock.minute.qfq.repair")
    parser.add_argument("--adjustflag", default="2", choices=["1", "2", "3"])
    parser.add_argument("--timeout-sec", type=int, default=90)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--sleep-seconds", type=float, default=0.05)
    parser.add_argument("--in-process-fetch", action="store_true",
                        help="Reuse one BaoStock login in this process. Fast, but no parent-side hard timeout.")
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--max-chunks", type=int, default=0)
    parser.add_argument(
        "--exclude-bj",
        action="store_true",
        default=True,
        help="Skip Beijing Stock Exchange codes because BaoStock minute API only accepts sh/sz.",
    )
    parser.add_argument(
        "--include-bj",
        dest="exclude_bj",
        action="store_false",
        help="Retry BJ codes as well. Intended only for diagnostics; BaoStock minute API normally rejects them.",
    )
    parser.add_argument("--report-every", type=int, default=10)
    parser.add_argument("--report", default="")
    return parser.parse_args()


def _ch() -> Any:
    return clickhouse_client()


def load_failed_5m_chunks(args: argparse.Namespace) -> pd.DataFrame:
    start = pd.Timestamp(args.start_date).date().isoformat()
    end = pd.Timestamp(args.end_date).date().isoformat()
    market_filter = "AND code NOT LIKE '%.BJ'" if args.exclude_bj else ""
    df = _ch().query_df(
        f"""
        SELECT code, toString(chunk_start) AS start_text, toString(chunk_end) AS end_text
        FROM {STATE_TABLE} FINAL
        WHERE status = 'failed'
          AND frequency = '5min'
          AND chunk_start >= toDate('{start}')
          AND chunk_start <= toDate('{end}')
          {market_filter}
        ORDER BY if(position(error, '用户未登录') > 0, 0, 1), code, chunk_start, chunk_end
        """
    )
    if df.empty:
        return df
    df = df.rename(columns={"start_text": "chunk_start", "end_text": "chunk_end"})
    df = df.drop_duplicates(["code", "chunk_start", "chunk_end"]).reset_index(drop=True)
    if args.shard_count > 1:
        df = df.iloc[[idx for idx in range(len(df)) if idx % args.shard_count == args.shard_index]].reset_index(drop=True)
    return df


def repair_one(row: Any, args: argparse.Namespace) -> dict[str, Any]:
    code = str(row.code)
    chunk_start = str(row.chunk_start)[:10]
    chunk_end = str(row.chunk_end)[:10]
    result: dict[str, Any] = {
        "code": code,
        "chunk_start": chunk_start,
        "chunk_end": chunk_end,
        "written": {"5min": 0, "15min": 0, "30min": 0, "60min": 0},
        "status": "ok",
        "error": "",
    }
    try:
        df_5m = fetch_one_with_retries(
            code=code,
            start_date=chunk_start,
            end_date=chunk_end,
            frequency="5",
            adjustflag=args.adjustflag,
            source_tag=args.source_tag,
            timeout_sec=args.timeout_sec,
            retries=args.retries,
            sleep_seconds=args.sleep_seconds,
            in_process_fetch=args.in_process_fetch,
        )
        df_5m_to_write = filter_missing_rows(FREQ_TABLE_MAP["5"], df_5m)
        written_5m = _upsert_to_ch(FREQ_TABLE_MAP["5"], df_5m_to_write, replace=False)
        mark_state(code, "5min", chunk_start, chunk_end, "ok", int(written_5m))
        result["written"]["5min"] = int(written_5m)

        local_5m = load_chunk(FREQ_TABLE_MAP["5"], code, chunk_start, chunk_end)
        for target_minutes in (15, 30, 60):
            frequency = f"{target_minutes}min"
            target_df = derive_higher_frame(local_5m, code, target_minutes)
            target_df = filter_missing_rows(FREQ_TABLE_MAP[str(target_minutes)], target_df)
            written = _upsert_to_ch(FREQ_TABLE_MAP[str(target_minutes)], target_df, replace=False)
            mark_state(code, frequency, chunk_start, chunk_end, "ok", int(written))
            result["written"][frequency] = int(written)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        mark_state(code, "5min", chunk_start, chunk_end, "failed", 0, error)
        result["status"] = "failed"
        result["error"] = error
    return result


def main() -> None:
    args = parse_args()
    if args.shard_count < 1:
        raise ValueError("--shard-count must be >= 1")
    if args.shard_index < 0 or args.shard_index >= args.shard_count:
        raise ValueError("--shard-index must be in [0, shard-count)")

    ensure_meta_tables()
    started = datetime.now()
    failed = load_failed_5m_chunks(args)
    print(json.dumps({
        "started_at": started.isoformat(timespec="seconds"),
        "failed_chunks": int(len(failed)),
        "shard_index": args.shard_index,
        "shard_count": args.shard_count,
    }, ensure_ascii=True), flush=True)

    bs_session = None
    if args.in_process_fetch:
        import baostock as bs

        login = bs.login()
        if getattr(login, "error_code", "0") != "0":
            raise RuntimeError(f"baostock login failed: {login.error_code} {login.error_msg}")
        bs_session = bs

    results: list[dict[str, Any]] = []
    try:
        for idx, row in enumerate(failed.itertuples(index=False), start=1):
            result = repair_one(row, args)
            results.append(result)
            if args.sleep_seconds > 0:
                time.sleep(args.sleep_seconds)
            if args.report_every and idx % args.report_every == 0:
                ok_count = sum(1 for item in results if item["status"] == "ok")
                failed_count = len(results) - ok_count
                print(json.dumps({
                    "progress": idx,
                    "ok": ok_count,
                    "failed": failed_count,
                    "last": result,
                }, ensure_ascii=True), flush=True)
            if args.max_chunks and idx >= args.max_chunks:
                break
    finally:
        if bs_session is not None:
            bs_session.logout()

    summary = {
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "shard_index": args.shard_index,
        "shard_count": args.shard_count,
        "input_chunks": int(len(failed)),
        "processed": len(results),
        "ok": sum(1 for item in results if item["status"] == "ok"),
        "failed": sum(1 for item in results if item["status"] != "ok"),
        "written": {
            frequency: sum(int(item["written"].get(frequency, 0)) for item in results)
            for frequency in ("5min", "15min", "30min", "60min")
        },
        "failures": [item for item in results if item["status"] != "ok"][:50],
    }
    print(json.dumps(summary, ensure_ascii=True), flush=True)
    report = Path(args.report) if args.report else None
    if report:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()

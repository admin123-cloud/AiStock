"""Fast intraday minute K-line sync from TdxQuant to ClickHouse.

This script is intentionally scoped to strategy-critical 15m/30m bars. It
pulls data in batches, validates complete market bar timestamps, and writes
deterministically into ClickHouse so G2 strategy refresh can self-repair
missing same-day minute data instead of blocking on a stale table.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data_fetcher.sources.tdxquant_pool import tdxquant_pool  # noqa: E402
from utils.market_warehouse import clickhouse_client, clickhouse_query_df  # noqa: E402


TABLE_BY_PERIOD = {
    "15m": "kline_minute_15",
    "30m": "kline_minute_30",
}
PERIOD_MINUTES = {
    "5m": 5,
    "15m": 15,
    "30m": 30,
}
COLUMNS = ["code", "datetime", "open", "high", "low", "close", "volume", "amount", "created_at", "id"]
DEFAULT_GATEWAY_BATCH_SIZE = int(os.environ.get("AISTOCK_INTRADAY_MINUTE_GATEWAY_BATCH_SIZE", "60"))
MAX_GATEWAY_COUNT_ALL_BATCH_SIZE = int(os.environ.get("AISTOCK_TDX_GATEWAY_MAX_COUNT_ALL_STOCKS", "80"))


def _log(message: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def _quote_sql(value: Any) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _chunked(items: list[str], size: int) -> Iterable[list[str]]:
    for idx in range(0, len(items), max(1, int(size))):
        yield items[idx : idx + max(1, int(size))]


def _stable_id(code: str, dt: pd.Timestamp, period: str) -> int:
    payload = f"{code}|{dt.strftime('%Y-%m-%d %H:%M:%S')}|{period}".encode("utf-8")
    return int.from_bytes(hashlib.blake2b(payload, digest_size=8).digest(), "big") & ((1 << 63) - 1)


def _market_bar_times(period: str) -> set[str]:
    minutes = PERIOD_MINUTES[period]
    times: set[str] = set()
    for start_text, end_text in (("09:30", "11:30"), ("13:00", "15:00")):
        cur = pd.Timestamp(f"2000-01-01 {start_text}") + pd.Timedelta(minutes=minutes)
        end = pd.Timestamp(f"2000-01-01 {end_text}")
        while cur <= end:
            times.add(cur.strftime("%H:%M"))
            cur += pd.Timedelta(minutes=minutes)
    return times


def _load_codes(types_arg: str, codes_arg: str, limit: int) -> list[str]:
    if codes_arg.strip():
        codes = [item.strip().upper() for item in codes_arg.split(",") if item.strip()]
    else:
        types = [item.strip() for item in types_arg.split(",") if item.strip()]
        if not types:
            types = ["stock", "index"]
        placeholders = ", ".join(["?"] * len(types))
        df = clickhouse_query_df(
            f"""
            SELECT code
            FROM stocks
            WHERE type IN ({placeholders})
              AND (quit = 0 OR quit IS NULL)
            ORDER BY type, code
            """,
            types,
        )
        codes = [] if df is None or df.empty else [str(x).strip().upper() for x in df["code"].tolist() if str(x).strip()]
    if limit > 0:
        codes = codes[: int(limit)]
    return list(dict.fromkeys(codes))


def _frame_for(data: dict[str, Any], field: str) -> pd.DataFrame:
    value = data.get(field)
    if isinstance(value, pd.DataFrame):
        return value.copy()
    return pd.DataFrame()


def _column_lookup(frame: pd.DataFrame) -> dict[str, Any]:
    lookup: dict[str, Any] = {}
    for col in frame.columns:
        text = str(col).strip().upper()
        if not text:
            continue
        lookup[text] = col
        lookup[text.split(".", 1)[0]] = col
    return lookup


def _resolve_column(lookup: dict[str, Any], code: str) -> Any:
    text = str(code).strip().upper()
    return lookup.get(text) or lookup.get(text.split(".", 1)[0])


def _market_data_to_frame(data: dict[str, Any], batch_codes: list[str], target_date: str, period: str) -> tuple[pd.DataFrame, dict[str, int]]:
    close_df = _frame_for(data, "Close")
    if close_df.empty:
        return pd.DataFrame(), {code: 0 for code in batch_codes}

    frames = {
        "open": _frame_for(data, "Open"),
        "high": _frame_for(data, "High"),
        "low": _frame_for(data, "Low"),
        "close": close_df,
        "volume": _frame_for(data, "Volume"),
        "amount": _frame_for(data, "Amount"),
    }
    lookups = {name: _column_lookup(frame) for name, frame in frames.items()}
    valid_times = _market_bar_times(period)
    created_at = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    rows: list[tuple] = []
    counts: dict[str, int] = {}

    for code in batch_codes:
        close_col = _resolve_column(lookups["close"], code)
        if close_col is None:
            counts[code] = 0
            continue
        one = pd.DataFrame(index=close_df.index)
        missing_core = False
        for name, frame in frames.items():
            col = _resolve_column(lookups[name], code)
            if frame.empty or col is None:
                if name in {"open", "high", "low", "close"}:
                    missing_core = True
                    break
                one[name] = 0.0
            else:
                one[name] = pd.to_numeric(frame[col], errors="coerce")
        if missing_core:
            counts[code] = 0
            continue

        one["datetime"] = pd.to_datetime(one.index, errors="coerce")
        one = one.dropna(subset=["datetime", "open", "high", "low", "close"]).copy()
        if one.empty:
            counts[code] = 0
            continue
        one = one[one["datetime"].dt.strftime("%Y-%m-%d") == target_date].copy()
        one = one[one["datetime"].dt.strftime("%H:%M").isin(valid_times)].copy()
        one = one.sort_values("datetime").drop_duplicates("datetime", keep="last")
        count = 0
        for item in one.itertuples(index=False):
            dt = pd.Timestamp(item.datetime).to_pydatetime()
            rows.append(
                (
                    code,
                    dt,
                    float(item.open),
                    float(item.high),
                    float(item.low),
                    float(item.close),
                    float(getattr(item, "volume", 0.0) or 0.0),
                    float(getattr(item, "amount", 0.0) or 0.0),
                    created_at,
                    _stable_id(code, pd.Timestamp(dt), period),
                )
            )
            count += 1
        counts[code] = count

    if not rows:
        return pd.DataFrame(), counts
    return pd.DataFrame(rows, columns=COLUMNS), counts


def _aggregate_5m_to_period(base: pd.DataFrame, target_period: str) -> pd.DataFrame:
    if base.empty or target_period not in {"15m", "30m"}:
        return pd.DataFrame()
    group_size = PERIOD_MINUTES[target_period] // PERIOD_MINUTES["5m"]
    rows: list[tuple] = []
    created_at = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    for code, code_df in base.groupby("code"):
        code_df = code_df.sort_values("datetime").reset_index(drop=True)
        for _, day_df in code_df.groupby(pd.to_datetime(code_df["datetime"], errors="coerce").dt.strftime("%Y-%m-%d")):
            day_df = day_df.reset_index(drop=True)
            for start_idx in range(0, len(day_df), group_size):
                chunk = day_df.iloc[start_idx : start_idx + group_size]
                if len(chunk) < group_size:
                    continue
                dt = pd.Timestamp(chunk.iloc[-1]["datetime"]).to_pydatetime()
                rows.append(
                    (
                        str(code),
                        dt,
                        float(chunk.iloc[0]["open"]),
                        float(pd.to_numeric(chunk["high"], errors="coerce").max()),
                        float(pd.to_numeric(chunk["low"], errors="coerce").min()),
                        float(chunk.iloc[-1]["close"]),
                        float(pd.to_numeric(chunk["volume"], errors="coerce").fillna(0).sum()),
                        float(pd.to_numeric(chunk["amount"], errors="coerce").fillna(0).sum()),
                        created_at,
                        _stable_id(str(code), pd.Timestamp(dt), target_period),
                    )
                )
    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows, columns=COLUMNS)
    valid_times = _market_bar_times(target_period)
    out = out[pd.to_datetime(out["datetime"], errors="coerce").dt.strftime("%H:%M").isin(valid_times)].copy()
    return out.sort_values(["code", "datetime"]).drop_duplicates(["code", "datetime"], keep="last")


def _fetch_period(batch_codes: list[str], target_date: str, period: str, fill_data: bool) -> tuple[pd.DataFrame, dict[str, int]]:
    ymd = target_date.replace("-", "")
    data = tdxquant_pool.get_market_data(
        field_list=["Open", "High", "Low", "Close", "Volume", "Amount"],
        stock_list=batch_codes,
        period=period,
        start_time=ymd,
        end_time=ymd,
        count=-1,
        dividend_type="none",
        fill_data=fill_data,
    )
    if not isinstance(data, dict):
        return pd.DataFrame(), {code: 0 for code in batch_codes}
    return _market_data_to_frame(data, batch_codes, target_date, period)


def _write_rows(table: str, rows: pd.DataFrame, target_date: str, dry_run: bool) -> int:
    if rows.empty:
        return 0
    client = clickhouse_client()
    codes = sorted({str(code) for code in rows["code"].dropna().tolist()})
    code_sql = ", ".join(_quote_sql(code) for code in codes)
    delete_sql = (
        f"ALTER TABLE {table} DELETE WHERE toDate(datetime) = toDate({_quote_sql(target_date)}) "
        f"AND code IN ({code_sql}) SETTINGS mutations_sync = 1"
    )
    if not dry_run:
        client.command(delete_sql)
        client.insert(table, [tuple(row) for row in rows.itertuples(index=False, name=None)], column_names=COLUMNS)
    return len(rows)


def _slot_status(table: str, target_date: str, min_codes: int) -> dict[str, Any]:
    df = clickhouse_query_df(
        f"""
        SELECT datetime AS dt, uniqExact(code) AS codes
        FROM {table}
        WHERE toDate(datetime) = toDate(?)
        GROUP BY dt
        ORDER BY dt DESC
        """,
        [target_date],
    )
    if df is None or df.empty:
        return {"ok": False, "latest_cutoff": None, "latest_codes": 0, "complete_cutoff": None, "complete_codes": 0}
    work = df.copy()
    work["codes"] = pd.to_numeric(work["codes"], errors="coerce").fillna(0).astype(int)
    latest = work.iloc[0]
    complete = work[work["codes"] >= int(min_codes)]
    return {
        "ok": not complete.empty,
        "latest_cutoff": pd.to_datetime(latest["dt"]).strftime("%Y-%m-%d %H:%M:%S"),
        "latest_codes": int(latest["codes"]),
        "complete_cutoff": None if complete.empty else pd.to_datetime(complete.iloc[0]["dt"]).strftime("%Y-%m-%d %H:%M:%S"),
        "complete_codes": 0 if complete.empty else int(complete.iloc[0]["codes"]),
        "min_codes": int(min_codes),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fast sync 15m/30m intraday bars from TdxQuant to ClickHouse")
    parser.add_argument("--target-date", default=datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d"))
    parser.add_argument("--periods", default="15m,30m", help="Comma separated periods: 15m,30m")
    parser.add_argument("--types", default="stock,index", help="Comma separated stock types")
    parser.add_argument("--codes", default="", help="Optional comma separated full codes")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_GATEWAY_BATCH_SIZE)
    parser.add_argument("--sleep", type=float, default=0.0)
    parser.add_argument("--min-complete-codes", type=int, default=3000)
    parser.add_argument("--fallback-from-5m", action="store_true", default=True)
    parser.add_argument("--no-fallback-from-5m", dest="fallback_from_5m", action="store_false")
    parser.add_argument("--fallback-from-snapshot", action="store_true", default=True)
    parser.add_argument("--no-fallback-from-snapshot", dest="fallback_from_snapshot", action="store_false")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    target_date = pd.Timestamp(args.target_date).strftime("%Y-%m-%d")
    periods = [p.strip().lower() for p in args.periods.split(",") if p.strip()]
    unknown = sorted(set(periods) - set(TABLE_BY_PERIOD))
    if unknown:
        raise SystemExit(f"Unsupported periods: {unknown}")

    codes = _load_codes(args.types, args.codes, args.limit)
    if not codes:
        raise SystemExit("No target codes loaded")
    requested_batch_size = max(1, int(args.batch_size or DEFAULT_GATEWAY_BATCH_SIZE))
    safe_batch_size = min(requested_batch_size, max(1, MAX_GATEWAY_COUNT_ALL_BATCH_SIZE))
    if safe_batch_size != requested_batch_size:
        _log(
            "cap batch_size for TDX Gateway count=-1 request: "
            f"requested={requested_batch_size} safe={safe_batch_size}"
        )
    _log(f"start target_date={target_date} codes={len(codes)} periods={periods} batch_size={safe_batch_size}")

    summary: dict[str, Any] = {
        "ok": True,
        "target_date": target_date,
        "codes": len(codes),
        "periods": {},
        "failed_batches": [],
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }
    started = time.perf_counter()

    for period in periods:
        table = TABLE_BY_PERIOD[period]
        period_rows = 0
        empty_codes: set[str] = set()
        batches = 0
        for batch in _chunked(codes, safe_batch_size):
            batches += 1
            batch_started = time.perf_counter()
            try:
                rows, counts = _fetch_period(batch, target_date, period, fill_data=False)
                if rows.empty and args.fallback_from_5m and period in {"15m", "30m"}:
                    base_rows, _ = _fetch_period(batch, target_date, "5m", fill_data=True)
                    rows = _aggregate_5m_to_period(base_rows, period)
                    counts = rows.groupby("code").size().to_dict() if not rows.empty else {code: 0 for code in batch}
                if rows.empty and args.fallback_from_snapshot and period in {"15m", "30m"}:
                    try:
                        from scripts.collect_intraday_snapshots import load_snapshot_minute_bars

                        snap = load_snapshot_minute_bars(
                            codes=batch,
                            start_date=target_date,
                            end_date=target_date,
                            period_minutes=PERIOD_MINUTES[period],
                            asset_type="stock",
                        )
                        if snap is not None and not snap.empty:
                            snap = snap.copy()
                            snap["created_at"] = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
                            snap["id"] = [
                                _stable_id(str(row.code), pd.Timestamp(row.datetime), period)
                                for row in snap.itertuples(index=False)
                            ]
                            rows = snap[COLUMNS].sort_values(["code", "datetime"]).drop_duplicates(
                                ["code", "datetime"], keep="last"
                            )
                            counts = rows.groupby("code").size().to_dict()
                    except Exception as exc:
                        _log(f"period={period} batch={batches} snapshot fallback failed {type(exc).__name__}: {exc}")
                inserted = _write_rows(table, rows, target_date, args.dry_run)
                period_rows += inserted
                empty_codes.update([code for code in batch if int(counts.get(code, 0) or 0) <= 0])
                _log(
                    f"period={period} batch={batches} codes={len(batch)} rows={inserted} "
                    f"empty={sum(1 for code in batch if int(counts.get(code, 0) or 0) <= 0)} "
                    f"elapsed={time.perf_counter() - batch_started:.2f}s"
                )
            except Exception as exc:
                summary["ok"] = False
                detail = {"period": period, "batch": batches, "codes": batch[:20], "error": f"{type(exc).__name__}: {exc}"}
                summary["failed_batches"].append(detail)
                _log(f"period={period} batch={batches} failed {detail['error']}")
            if args.sleep > 0:
                time.sleep(float(args.sleep))

        status = _slot_status(table, target_date, args.min_complete_codes) if not args.dry_run else {}
        if period == "30m" and not bool(status.get("ok", False)) and not args.dry_run:
            summary["ok"] = False
        summary["periods"][period] = {
            "table": table,
            "rows_written": period_rows,
            "empty_codes": len(empty_codes),
            "empty_code_sample": sorted(empty_codes)[:80],
            "batches": batches,
            "slot_status": status,
        }

    summary["finished_at"] = datetime.now().isoformat(timespec="seconds")
    summary["duration_seconds"] = round(time.perf_counter() - started, 3)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0 if summary["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

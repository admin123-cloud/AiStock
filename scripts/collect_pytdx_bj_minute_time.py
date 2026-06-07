"""Fill missing stock minute days from pytdx history minute-time data.

This is a fallback for the old BJ/920 history window where pytdx native 5m
bars are not available.  TDX returns 240 one-minute close/volume points per
day.  The script aggregates them into 5m bars and derives 15/30/60m bars.
"""

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

from scripts.collect_baostock_all_minutes import FREQ_TABLE_MAP, _upsert_to_ch, derive_higher_frame  # noqa: E402
from utils.market_warehouse import clickhouse_client  # noqa: E402


DEFAULT_HOSTS = [
    "180.153.18.170:7709",
    "123.125.108.90:7709",
    "47.103.48.45:7709",
    "119.147.212.81:7709",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fill missing stock days from pytdx minute-time data.")
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--codes", default="")
    parser.add_argument("--markets", default="BJ", help="Comma-separated markets: SH,SZ,BJ.")
    parser.add_argument("--asset-types", default="stock", help="Comma-separated stock/index asset types.")
    parser.add_argument("--limit-days", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--include-partial", action="store_true",
                        help="Also repair days with 1..47 existing 5m bars.")
    parser.add_argument("--replace-days", action="store_true",
                        help="Delete existing rows for repaired code-days before inserting regenerated bars.")
    parser.add_argument("--hosts", default=",".join(DEFAULT_HOSTS))
    parser.add_argument("--sleep-seconds", type=float, default=0.02)
    parser.add_argument("--batch-days", type=int, default=200)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report-every", type=int, default=200)
    parser.add_argument("--report", default="")
    return parser.parse_args()


def _parse_hosts(value: str) -> list[tuple[str, int]]:
    hosts: list[tuple[str, int]] = []
    for item in str(value or "").split(","):
        item = item.strip()
        if not item:
            continue
        host, _, port = item.partition(":")
        hosts.append((host, int(port or 7709)))
    return hosts


def _connect(hosts: list[tuple[str, int]]) -> Any:
    from pytdx.hq import TdxHq_API

    last_error: Exception | None = None
    for host, port in hosts:
        api = TdxHq_API(raise_exception=True, auto_retry=True)
        try:
            if api.connect(host, port, time_out=5):
                return api
        except Exception as exc:
            last_error = exc
            try:
                api.disconnect()
            except Exception:
                pass
    raise RuntimeError(f"pytdx connect failed: {last_error}")


def _stable_id(code: str, dt: pd.Timestamp, period: str) -> int:
    key = f"{code}|{dt:%Y-%m-%d %H:%M:%S}|{period}|pytdx.minute_time.qfq".encode("utf-8")
    return int.from_bytes(hashlib.blake2b(key, digest_size=8).digest(), "big", signed=False) % (2**63)


def _minute_times(day: pd.Timestamp) -> list[pd.Timestamp]:
    times: list[pd.Timestamp] = []
    current = day.replace(hour=9, minute=31, second=0, microsecond=0)
    while current <= day.replace(hour=11, minute=30, second=0, microsecond=0):
        times.append(current)
        current += pd.Timedelta(minutes=1)
    current = day.replace(hour=13, minute=1, second=0, microsecond=0)
    while current <= day.replace(hour=15, minute=0, second=0, microsecond=0):
        times.append(current)
        current += pd.Timedelta(minutes=1)
    return times


def _load_missing_days(start_date: str, end_date: str, codes: list[str], markets: list[str],
                       asset_types: list[str],
                       include_partial: bool, shard_count: int, shard_index: int,
                       limit: int) -> pd.DataFrame:
    code_filter = ""
    shard_filter = ""
    params: dict[str, Any] = {"markets": tuple(markets), "asset_types": tuple(asset_types)}
    if codes:
        code_filter = "AND d.code IN %(codes)s"
        params["codes"] = tuple(codes)
    if shard_count > 1:
        shard_filter = "AND modulo(cityHash64(d.code, toString(d.trade_date)), %(shard_count)s) = %(shard_index)s"
        params["shard_count"] = int(shard_count)
        params["shard_index"] = int(shard_index)
    frames: list[pd.DataFrame] = []
    chunk_start = pd.Timestamp(start_date)
    final_day = pd.Timestamp(end_date)
    while chunk_start <= final_day:
        chunk_end = min(chunk_start + pd.offsets.MonthEnd(0), final_day)
        daily = clickhouse_client().query_df(
            f"""
            SELECT d.code, d.trade_date
            FROM kline_daily d INNER JOIN stocks s ON d.code=s.code
            WHERE s.type IN %(asset_types)s AND s.market IN %(markets)s
              AND d.trade_date >= toDate(%(start)s)
              AND d.trade_date <= toDate(%(end)s)
              {code_filter}
              {shard_filter}
            GROUP BY d.code, d.trade_date
            ORDER BY d.code, d.trade_date
            """,
            parameters={
                **params,
                "start": chunk_start.strftime("%Y-%m-%d"),
                "end": chunk_end.strftime("%Y-%m-%d"),
            },
        )
        if not daily.empty:
            counts: list[pd.DataFrame] = []
            month_codes = sorted(str(code) for code in daily["code"].dropna().unique().tolist())
            for offset in range(0, len(month_codes), 100):
                batch_codes = month_codes[offset:offset + 100]
                if not batch_codes:
                    continue
                counts.append(clickhouse_client().query_df(
                    """
                    SELECT code, toDate(datetime) AS trade_date, count() AS bars
                    FROM kline_minute_5
                    WHERE code IN %(codes)s
                      AND datetime >= toDateTime(%(start)s)
                      AND datetime < toDateTime(%(end)s) + INTERVAL 1 DAY
                    GROUP BY code, trade_date
                    """,
                    parameters={
                        "codes": tuple(batch_codes),
                        "start": chunk_start.strftime("%Y-%m-%d"),
                        "end": chunk_end.strftime("%Y-%m-%d"),
                    },
                ))
            existing = pd.concat(counts, ignore_index=True) if counts else pd.DataFrame(columns=["code", "trade_date", "bars"])
            existing = existing.reindex(columns=["code", "trade_date", "bars"])
            df = daily.merge(existing, on=["code", "trade_date"], how="left")
            df["bars"] = df["bars"].fillna(0).astype(int)
            if include_partial:
                df = df[(df["bars"] == 0) | ((df["bars"] > 0) & (df["bars"] < 48))]
            else:
                df = df[df["bars"] == 0]
            df = df[["code", "trade_date"]]
            if not df.empty:
                frames.append(df)
            if limit and sum(len(frame) for frame in frames) >= limit:
                break
        chunk_start = chunk_end + pd.Timedelta(days=1)

    if not frames:
        return pd.DataFrame(columns=["code", "trade_date"])
    result = pd.concat(frames, ignore_index=True).sort_values(["code", "trade_date"]).reset_index(drop=True)
    if limit and limit > 0:
        result = result.head(int(limit))
    return result


def _month_ranges(start_date: str, end_date: str) -> list[tuple[str, str]]:
    ranges: list[tuple[str, str]] = []
    chunk_start = pd.Timestamp(start_date)
    final_day = pd.Timestamp(end_date)
    while chunk_start <= final_day:
        chunk_end = min(chunk_start + pd.offsets.MonthEnd(0), final_day)
        ranges.append((chunk_start.strftime("%Y-%m-%d"), chunk_end.strftime("%Y-%m-%d")))
        chunk_start = chunk_end + pd.Timedelta(days=1)
    return ranges


def _daily_qfq_close(codes: list[str], start_date: str, end_date: str) -> dict[tuple[str, pd.Timestamp], float]:
    rows = clickhouse_client().query(
        """
        SELECT code, trade_date, close
        FROM kline_daily
        WHERE code IN %(codes)s AND trade_date >= toDate(%(start)s) AND trade_date <= toDate(%(end)s)
        """,
        parameters={"codes": tuple(codes), "start": start_date, "end": end_date},
    ).result_rows
    return {(str(row[0]), pd.Timestamp(row[1])): float(row[2]) for row in rows if row[2] and float(row[2]) > 0}


def _pytdx_market(code: str) -> int:
    if code.endswith(".SH"):
        return 1
    if code.endswith(".SZ"):
        return 0
    if code.endswith(".BJ"):
        return 2
    raise ValueError(f"unsupported code suffix for pytdx: {code}")


def _delete_existing_day(code: str, day: pd.Timestamp, dry_run: bool) -> None:
    if dry_run:
        return
    ch = clickhouse_client()
    start = day.strftime("%Y-%m-%d 00:00:00")
    end = (day + pd.Timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")
    for table in FREQ_TABLE_MAP.values():
        ch.command(
            f"ALTER TABLE {table} DELETE WHERE code = %(code)s "
            "AND datetime >= toDateTime(%(start)s) AND datetime < toDateTime(%(end)s) "
            "SETTINGS mutations_sync = 1",
            parameters={"code": code, "start": start, "end": end},
        )


def _frame_from_minute_time(code: str, day: pd.Timestamp, rows: list[dict[str, Any]],
                            qfq_close: float | None) -> pd.DataFrame:
    if len(rows) < 240:
        return pd.DataFrame()
    df = pd.DataFrame(rows[:240])
    if df.empty or "price" not in df.columns or "vol" not in df.columns:
        return pd.DataFrame()
    df["datetime"] = _minute_times(day)[: len(df)]
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["vol"] = pd.to_numeric(df["vol"], errors="coerce").fillna(0.0)
    df = df.dropna(subset=["price"])
    if len(df) < 240:
        return pd.DataFrame()

    factor = 1.0
    raw_close = float(df["price"].iloc[-1] or 0)
    if qfq_close and raw_close > 0:
        factor = float(qfq_close) / raw_close

    df["price"] = (df["price"].astype(float) * factor).round(4)
    df["volume"] = df["vol"].astype(float) * 100.0
    df["amount"] = (df["price"].astype(float) * df["volume"]).round(4)
    df["_bucket"] = df.groupby(df["datetime"].dt.date).cumcount() // 5
    grouped = df.groupby("_bucket", as_index=False)
    out = grouped.agg(
        datetime=("datetime", "max"),
        open=("price", "first"),
        high=("price", "max"),
        low=("price", "min"),
        close=("price", "last"),
        volume=("volume", "sum"),
        amount=("amount", "sum"),
    )
    out["code"] = code
    out["created_at"] = datetime.now()
    out["id"] = [_stable_id(code, pd.Timestamp(dt), "5m") for dt in out["datetime"]]
    return out[["code", "datetime", "open", "high", "low", "close", "volume", "amount", "created_at", "id"]]


def _flush(frames_5m: list[pd.DataFrame], dry_run: bool) -> dict[str, int]:
    written = {"5m": 0, "15m": 0, "30m": 0, "60m": 0}
    if not frames_5m:
        return written
    df_5m = pd.concat(frames_5m, ignore_index=True)
    if df_5m.empty:
        return written
    if dry_run:
        written["5m"] = int(len(df_5m))
    else:
        written["5m"] = _upsert_to_ch(FREQ_TABLE_MAP["5"], df_5m, replace=False)
    for minutes in (15, 30, 60):
        frames = []
        for code, code_df in df_5m.groupby("code"):
            for day, day_df in code_df.groupby(pd.to_datetime(code_df["datetime"]).dt.date):
                frames.append(derive_higher_frame(day_df, str(code), minutes))
        df_target = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        key = f"{minutes}m"
        if dry_run:
            written[key] = int(len(df_target))
        elif not df_target.empty:
            written[key] = _upsert_to_ch(FREQ_TABLE_MAP[str(minutes)], df_target, replace=False)
    return written


def main() -> int:
    args = parse_args()
    codes = [item.strip().upper() for item in args.codes.split(",") if item.strip()]
    markets = [item.strip().upper() for item in args.markets.split(",") if item.strip()]
    asset_types = [item.strip().lower() for item in args.asset_types.split(",") if item.strip()]
    if not asset_types:
        raise ValueError("--asset-types must not be empty")
    if args.shard_count < 1:
        raise ValueError("--shard-count must be >= 1")
    if args.shard_index < 0 or args.shard_index >= args.shard_count:
        raise ValueError("--shard-index must be in [0, shard-count)")
    api = _connect(_parse_hosts(args.hosts))
    started = datetime.now()
    summary = {
        "started_at": started.isoformat(timespec="seconds"),
        "start_date": args.start_date,
        "end_date": args.end_date,
        "input_days": 0,
        "shard_index": args.shard_index,
        "shard_count": args.shard_count,
        "processed_days": 0,
        "empty_days": 0,
        "source_1m_rows": 0,
        "written": {"5m": 0, "15m": 0, "30m": 0, "60m": 0},
        "failures": [],
        "dry_run": bool(args.dry_run),
    }
    pending: list[pd.DataFrame] = []
    try:
        remaining_limit = int(args.limit_days or 0)
        for month_start, month_end in _month_ranges(args.start_date, args.end_date):
            if remaining_limit and summary["input_days"] >= remaining_limit:
                break
            month_limit = max(0, remaining_limit - int(summary["input_days"])) if remaining_limit else 0
            missing = _load_missing_days(
                month_start,
                month_end,
                codes,
                markets,
                asset_types,
                args.include_partial,
                args.shard_count,
                args.shard_index,
                month_limit,
            )
            if missing.empty:
                continue
            missing["trade_date"] = pd.to_datetime(missing["trade_date"])
            summary["input_days"] += int(len(missing))
            codes_to_fetch = sorted(set(str(item) for item in missing["code"].tolist()))
            qfq_closes = _daily_qfq_close(codes_to_fetch, month_start, month_end)
            print(json.dumps({
                "scan_month": {"start": month_start, "end": month_end},
                "month_missing_days": int(len(missing)),
                "input_days": summary["input_days"],
                "processed_days": summary["processed_days"],
            }, ensure_ascii=False), flush=True)
            for row in missing.itertuples(index=False):
                code = str(row.code)
                day = pd.Timestamp(row.trade_date)
                try:
                    raw_code = code.split(".", 1)[0]
                    rows = api.get_history_minute_time_data(_pytdx_market(code), raw_code, int(day.strftime("%Y%m%d"))) or []
                    summary["source_1m_rows"] += int(len(rows))
                    df_5m = _frame_from_minute_time(code, day, rows, qfq_closes.get((code, day)))
                    if df_5m.empty:
                        summary["empty_days"] += 1
                    else:
                        if args.replace_days:
                            _delete_existing_day(code, day, args.dry_run)
                        pending.append(df_5m)
                    summary["processed_days"] += 1
                    if len(pending) >= args.batch_days:
                        written = _flush(pending, args.dry_run)
                        pending.clear()
                        for key, value in written.items():
                            summary["written"][key] += int(value)
                    if args.report_every and summary["processed_days"] % args.report_every == 0:
                        print(json.dumps({
                            "processed_days": summary["processed_days"],
                            "written": summary["written"],
                            "empty_days": summary["empty_days"],
                            "last": {"code": code, "trade_date": day.date().isoformat()},
                        }, ensure_ascii=False), flush=True)
                    if args.sleep_seconds > 0:
                        time.sleep(args.sleep_seconds)
                except Exception as exc:
                    summary["failures"].append({
                        "code": code,
                        "trade_date": day.date().isoformat(),
                        "error": f"{type(exc).__name__}: {exc}",
                    })
        written = _flush(pending, args.dry_run)
        for key, value in written.items():
            summary["written"][key] += int(value)
    finally:
        try:
            api.disconnect()
        except Exception:
            pass
    if summary["input_days"] == 0:
        print(json.dumps({"message": "no missing days", "start_date": args.start_date, "end_date": args.end_date}, ensure_ascii=False))
        return 0
    summary["finished_at"] = datetime.now().isoformat(timespec="seconds")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    if args.report:
        path = Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if not summary["failures"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

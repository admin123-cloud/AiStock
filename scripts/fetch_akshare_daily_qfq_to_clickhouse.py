"""Fetch A-share qfq daily bars from AkShare into ClickHouse.

This table is primarily used as an adjustment-factor source for local TDX
minute bars.  It is kept separate from kline_daily so the existing application
daily table is not overwritten while we validate the factor pipeline.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing as mp
import random
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.kline_units import (  # noqa: E402
    normalize_akshare_daily_units,
    normalize_baostock_daily_units,
)
from utils.market_warehouse import clickhouse_client  # noqa: E402


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
    "id",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch qfq daily bars from AkShare to ClickHouse.")
    parser.add_argument("--start-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--target-table", default="kline_daily_qfq")
    parser.add_argument("--provider", choices=["baostock", "akshare"], default="baostock")
    parser.add_argument("--codes", default="", help="Optional full codes: 000001.SZ,600000.SH")
    parser.add_argument("--asset-type", choices=["stock", "all"], default="stock")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--sleep", type=float, default=0.03)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--worker-timeout", type=float, default=0.0, help="Use child-process timeout per baostock code when > 0.")
    parser.add_argument("--batch-rows", type=int, default=50_000)
    parser.add_argument("--replace-table", action="store_true")
    parser.add_argument("--progress-every", type=int, default=50)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report-path", default="")
    return parser.parse_args()


def _stable_id(code: str, trade_date: object) -> int:
    key = f"{code}|{trade_date}|qfq_daily".encode("utf-8")
    return int.from_bytes(hashlib.blake2b(key, digest_size=8).digest(), "big", signed=False)


def _create_table(client, table: str, replace: bool, dry_run: bool) -> None:
    if replace:
        print(f"drop_table table={table} dry_run={dry_run}", flush=True)
        if not dry_run:
            client.command(f"DROP TABLE IF EXISTS {table}")
    sql = f"""
    CREATE TABLE IF NOT EXISTS {table}
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
    ENGINE = ReplacingMergeTree
    ORDER BY (code, trade_date)
    """
    if not dry_run:
        client.command(sql)


def _load_codes(client, asset_type: str, explicit_codes: str, limit: int) -> list[str]:
    if explicit_codes.strip():
        codes = [item.strip().upper() for item in explicit_codes.split(",") if item.strip()]
        return codes[:limit] if limit else codes
    type_filter = "type = 'stock'" if asset_type == "stock" else "type IN ('stock', 'index')"
    rows = client.query(
        f"""
        SELECT code
        FROM stocks
        WHERE ({type_filter})
          AND (quit = 0 OR quit IS NULL)
          AND market IN ('SH', 'SZ', 'sh', 'sz')
        ORDER BY code
        """
    ).result_rows
    codes = [str(row[0]).strip().upper() for row in rows if row and row[0]]
    return codes[:limit] if limit else codes


def _raw_symbol(code: str) -> str:
    return str(code).split(".", 1)[0]


def _baostock_code(code: str) -> str:
    raw = _raw_symbol(code)
    suffix = str(code).split(".")[-1].upper() if "." in str(code) else ""
    if suffix in {"SH", "SZ"}:
        return f"{suffix.lower()}.{raw}"
    if raw.startswith(("60", "68", "90", "000")):
        return f"sh.{raw}"
    return f"sz.{raw}"


def _fetch_one_akshare(ak, code: str, start_date: str, end_date: str, retries: int) -> pd.DataFrame:
    last_error: Exception | None = None
    for attempt in range(1, max(1, retries) + 1):
        try:
            df = ak.stock_zh_a_hist(
                symbol=_raw_symbol(code),
                period="daily",
                start_date=start_date.replace("-", ""),
                end_date=end_date.replace("-", ""),
                adjust="qfq",
            )
            break
        except Exception as exc:
            last_error = exc
            wait = min(20.0, 1.5 * attempt + random.random())
            print(f"retry code={code} attempt={attempt}/{retries} wait={wait:.1f}s error={type(exc).__name__}: {exc}", flush=True)
            time.sleep(wait)
    else:
        if last_error is not None:
            raise last_error
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    mapping = {
        "日期": "trade_date",
        "开盘": "open",
        "最高": "high",
        "最低": "low",
        "收盘": "close",
        "成交量": "volume",
        "成交额": "amount",
        "振幅": "amplitude",
        "涨跌幅": "change_pct",
        "涨跌额": "change_amount",
        "换手率": "turnover_rate",
    }
    work = df.rename(columns=mapping)
    required = ["trade_date", "open", "high", "low", "close"]
    if any(col not in work.columns for col in required):
        return pd.DataFrame()
    work["trade_date"] = pd.to_datetime(work["trade_date"], errors="coerce").dt.date
    for col in [
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
    ]:
        if col not in work.columns:
            work[col] = 0.0
        work[col] = pd.to_numeric(work[col], errors="coerce").fillna(0.0)
    work = work.dropna(subset=["trade_date", "open", "high", "low", "close"])
    work = work[(work["open"] > 0) & (work["high"] > 0) & (work["low"] > 0) & (work["close"] > 0)]
    if work.empty:
        return pd.DataFrame()
    work = normalize_akshare_daily_units(work)
    work["code"] = code
    return work[[
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
    ]].sort_values("trade_date")


def _fetch_one_baostock(bs, code: str, start_date: str, end_date: str, retries: int) -> pd.DataFrame:
    last_error: Exception | None = None
    fields = "date,code,open,high,low,close,volume,amount,adjustflag"
    for attempt in range(1, max(1, retries) + 1):
        try:
            rs = bs.query_history_k_data_plus(
                _baostock_code(code),
                fields,
                start_date=start_date,
                end_date=end_date,
                frequency="d",
                adjustflag="2",
            )
            if getattr(rs, "error_code", "0") != "0":
                raise RuntimeError(f"baostock query failed: {rs.error_code} {rs.error_msg}")
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            if not rows:
                return pd.DataFrame()
            df = pd.DataFrame(rows, columns=rs.fields)
            break
        except Exception as exc:
            last_error = exc
            wait = min(20.0, 1.5 * attempt + random.random())
            print(f"retry code={code} attempt={attempt}/{retries} wait={wait:.1f}s error={type(exc).__name__}: {exc}", flush=True)
            time.sleep(wait)
    else:
        if last_error is not None:
            raise last_error
        return pd.DataFrame()

    work = df.rename(columns={"date": "trade_date"})
    work["trade_date"] = pd.to_datetime(work["trade_date"], errors="coerce").dt.date
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        work[col] = pd.to_numeric(work[col], errors="coerce").fillna(0.0)
    work["amplitude"] = 0.0
    work["change_pct"] = 0.0
    work["change_amount"] = 0.0
    work["turnover_rate"] = 0.0
    work = work.dropna(subset=["trade_date", "open", "high", "low", "close"])
    work = work[(work["open"] > 0) & (work["high"] > 0) & (work["low"] > 0) & (work["close"] > 0)]
    if work.empty:
        return pd.DataFrame()
    work = normalize_baostock_daily_units(work)
    work["code"] = code
    return work[[
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
    ]].sort_values("trade_date")


def _baostock_child(queue: mp.Queue, code: str, start_date: str, end_date: str) -> None:
    try:
        import baostock as bs

        login_result = bs.login()
        if getattr(login_result, "error_code", "0") != "0":
            queue.put({"ok": False, "error": f"login failed: {login_result.error_code} {login_result.error_msg}"})
            return
        try:
            fields = "date,code,open,high,low,close,volume,amount,adjustflag"
            rs = bs.query_history_k_data_plus(
                _baostock_code(code),
                fields,
                start_date=start_date,
                end_date=end_date,
                frequency="d",
                adjustflag="2",
            )
            if getattr(rs, "error_code", "0") != "0":
                queue.put({"ok": False, "error": f"query failed: {rs.error_code} {rs.error_msg}"})
                return
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            queue.put({"ok": True, "fields": rs.fields, "rows": rows})
        finally:
            try:
                bs.logout()
            except Exception:
                pass
    except Exception as exc:
        queue.put({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


def _fetch_one_baostock_timeout(code: str, start_date: str, end_date: str, timeout_seconds: float) -> pd.DataFrame:
    queue: mp.Queue = mp.Queue(maxsize=1)
    proc = mp.Process(target=_baostock_child, args=(queue, code, start_date, end_date))
    proc.start()
    proc.join(timeout_seconds)
    if proc.is_alive():
        proc.terminate()
        proc.join(5)
        raise TimeoutError(f"baostock timeout after {timeout_seconds}s")
    if queue.empty():
        raise RuntimeError(f"baostock worker exited with code {proc.exitcode} and no result")
    payload = queue.get()
    if not payload.get("ok"):
        raise RuntimeError(str(payload.get("error") or "baostock worker failed"))
    rows = payload.get("rows") or []
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=payload.get("fields") or [])
    return _normalize_baostock_frame(df, code)


def _normalize_baostock_frame(df: pd.DataFrame, code: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    work = df.rename(columns={"date": "trade_date"})
    work["trade_date"] = pd.to_datetime(work["trade_date"], errors="coerce").dt.date
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        work[col] = pd.to_numeric(work[col], errors="coerce").fillna(0.0)
    work["amplitude"] = 0.0
    work["change_pct"] = 0.0
    work["change_amount"] = 0.0
    work["turnover_rate"] = 0.0
    work = work.dropna(subset=["trade_date", "open", "high", "low", "close"])
    work = work[(work["open"] > 0) & (work["high"] > 0) & (work["low"] > 0) & (work["close"] > 0)]
    if work.empty:
        return pd.DataFrame()
    work = normalize_baostock_daily_units(work)
    work["code"] = code
    return work[[
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
    ]].sort_values("trade_date")


def _rows_from_frame(df: pd.DataFrame, created_at: datetime) -> list[tuple]:
    rows: list[tuple] = []
    for item in df.itertuples(index=False):
        code = str(item.code)
        trade_date = item.trade_date
        rows.append(
            (
                code,
                trade_date,
                float(item.open),
                float(item.high),
                float(item.low),
                float(item.close),
                float(item.volume),
                float(item.amount),
                float(item.amplitude),
                float(item.change_pct),
                float(item.change_amount),
                float(item.turnover_rate),
                created_at,
                _stable_id(code, trade_date),
            )
        )
    return rows


def _flush(client, table: str, rows: list[tuple], dry_run: bool) -> int:
    if not rows:
        return 0
    if not dry_run:
        client.insert(table, rows, column_names=COLUMNS)
    return len(rows)


def main() -> int:
    args = parse_args()
    ak = None
    bs = None
    if args.provider == "akshare":
        import akshare as ak  # type: ignore
    else:
        import baostock as bs  # type: ignore
        login_result = bs.login()
        if getattr(login_result, "error_code", "0") != "0":
            raise RuntimeError(f"baostock login failed: {login_result.error_code} {login_result.error_msg}")

    client = clickhouse_client()
    _create_table(client, args.target_table, args.replace_table, args.dry_run)
    codes = _load_codes(client, args.asset_type, args.codes, args.limit)
    print(
        f"start target={args.target_table} codes={len(codes)} range={args.start_date}~{args.end_date} "
        f"provider={args.provider} replace={args.replace_table} dry_run={args.dry_run}",
        flush=True,
    )
    stats = {
        "target_table": args.target_table,
        "codes": len(codes),
        "requested": 0,
        "fetched": 0,
        "empty": 0,
        "failed": 0,
        "written": 0,
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }
    pending: list[tuple] = []
    started = time.time()
    created_at = datetime.now() + timedelta(hours=8)

    for idx, code in enumerate(codes, start=1):
        stats["requested"] += 1
        try:
            if args.provider == "akshare":
                df = _fetch_one_akshare(ak, code, args.start_date, args.end_date, args.retries)
            elif args.worker_timeout and args.worker_timeout > 0:
                df = _fetch_one_baostock_timeout(code, args.start_date, args.end_date, args.worker_timeout)
            else:
                df = _fetch_one_baostock(bs, code, args.start_date, args.end_date, args.retries)
            if df.empty:
                stats["empty"] += 1
            else:
                stats["fetched"] += 1
                pending.extend(_rows_from_frame(df, created_at))
                if len(pending) >= args.batch_rows:
                    stats["written"] += _flush(client, args.target_table, pending, args.dry_run)
                    pending.clear()
        except Exception as exc:
            stats["failed"] += 1
            print(f"failed code={code} error={type(exc).__name__}: {exc}", flush=True)
        if args.sleep > 0:
            time.sleep(args.sleep)
        if args.progress_every and idx % args.progress_every == 0:
            print(
                f"progress codes={idx}/{len(codes)} fetched={stats['fetched']} empty={stats['empty']} "
                f"failed={stats['failed']} written={stats['written']} elapsed={time.time() - started:.1f}s",
                flush=True,
            )

    stats["written"] += _flush(client, args.target_table, pending, args.dry_run)
    pending.clear()
    stats["finished_at"] = datetime.now().isoformat(timespec="seconds")
    stats["elapsed_seconds"] = round(time.time() - started, 3)
    print("done " + json.dumps(stats, ensure_ascii=False, default=str), flush=True)
    if args.report_path:
        report_path = Path(args.report_path)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if bs is not None:
        try:
            bs.logout()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

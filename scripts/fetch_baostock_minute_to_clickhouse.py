"""
Fetch A-share minute bars from baostock into ClickHouse kline_minute_* tables.

ClickHouse-only Baostock minute fetcher.
Writes directly to ClickHouse kline_minute_5 / 15 / 30 / 60 tables.

Usage:
    python scripts/fetch_baostock_minute_to_clickhouse.py \
        --codes 000001.SZ,600000.SH --start-date 2025-01-01 --end-date 2025-01-31
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from utils.market_warehouse import clickhouse_client
from utils.kline_store import filter_trading_day_rows

EXPORTS_DIR = REPO_ROOT / "data" / "warehouse" / "exports"

# frequency arg → target ClickHouse table
FREQ_TABLE_MAP = {
    "5": "kline_minute_5",
    "15": "kline_minute_15",
    "30": "kline_minute_30",
    "60": "kline_minute_60",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch A-share minute bars from baostock into ClickHouse."
    )
    parser.add_argument("--codes", required=True,
                        help="Comma separated codes: 000001.SZ,600000.SH,000001.SH.")
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--frequency", default="5", choices=["5", "15", "30", "60"],
                        help="Baostock minute frequency.")
    parser.add_argument("--adjustflag", default="2", choices=["1", "2", "3"],
                        help="1=后复权, 2=前复权, 3=不复权.")
    parser.add_argument("--source-tag", default="baostock.minute.qfq")
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--sleep-seconds", type=float, default=0.2)
    parser.add_argument("--timeout-sec", type=int, default=45,
                        help="Timeout per code request; timed-out workers are terminated.")
    parser.add_argument("--report", default="")
    return parser.parse_args()


def normalize_code(code: str) -> str:
    item = code.strip().upper()
    if "." in item:
        bare, suffix = item.split(".", 1)
        return f"{bare.zfill(6)}.{suffix.upper()}"
    if item.startswith(("SH", "SZ", "BJ")):
        return f"{item[2:8]}.{item[:2]}"
    if item.startswith(("6", "5", "9")):
        return f"{item}.SH"
    if item.startswith(("4", "8")):
        return f"{item}.BJ"
    return f"{item}.SZ"


def baostock_code(code: str) -> str:
    bare, suffix = code.upper().split(".", 1)
    return f"{suffix.lower()}.{bare}"


def fetch_one(code: str, start_date: str, end_date: str,
              frequency: str, adjustflag: str, source_tag: str) -> pd.DataFrame:
    import baostock as bs
    fields = "date,time,code,open,high,low,close,volume,amount,adjustflag"
    rs = bs.query_history_k_data_plus(
        baostock_code(code),
        fields,
        start_date=start_date,
        end_date=end_date,
        frequency=frequency,
        adjustflag=adjustflag,
    )
    rows: List[List[str]] = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    if rs.error_code != "0":
        raise RuntimeError(f"{code}: {rs.error_msg}")
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=rs.fields)
    df["code"] = code
    time_text = df["time"].astype(str).str.slice(0, 14)
    df["datetime"] = pd.to_datetime(time_text, format="%Y%m%d%H%M%S", errors="coerce")
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df[(df["open"] > 0) & (df["high"] > 0) & (df["low"] > 0) & (df["close"] > 0)].copy()
    if df.empty:
        return pd.DataFrame()
    # Keep the exchange business timestamp as Asia/Shanghai wall-clock time.
    # baostock volume is in shares; kline_minute_* stores volume in 手 (1手=100股)
    df["volume"] = (df["volume"] / 100).round(0).astype("int64")
    # Keep amount unit consistent with TdxQuant/TDX local imports: 10k CNY.
    df["amount"] = (df["amount"] / 10000.0).round(4)
    df["created_at"] = datetime.now()
    result = df[["code", "datetime", "open", "high", "low", "close", "volume", "amount", "created_at"]].copy()
    result["id"] = [
        abs(hash((row.code, int(row.datetime.timestamp()), f"{frequency}m", "baostock"))) % (2**63)
        for row in result.itertuples(index=False)
    ]
    return result.dropna(subset=["datetime", "open", "high", "low", "close"])


def _baostock_minute_worker(
    queue: mp.Queue,
    code: str,
    start_date: str,
    end_date: str,
    frequency: str,
    adjustflag: str,
    source_tag: str,
) -> None:
    import baostock as bs
    login = bs.login()
    if login.error_code != "0":
        queue.put({"ok": False, "error": f"baostock login failed: {login.error_msg}"})
        return
    try:
        df = fetch_one(code, start_date, end_date, frequency, adjustflag, source_tag)
        queue.put({"ok": True, "records": df.to_dict("records")})
    except Exception as exc:
        queue.put({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
    finally:
        bs.logout()


def fetch_one_timeout(
    code: str,
    start_date: str,
    end_date: str,
    frequency: str,
    adjustflag: str,
    source_tag: str,
    timeout_sec: int,
) -> pd.DataFrame:
    queue: mp.Queue = mp.Queue()
    proc = mp.Process(
        target=_baostock_minute_worker,
        args=(queue, code, start_date, end_date, frequency, adjustflag, source_tag),
    )
    proc.start()
    proc.join(timeout_sec)
    if proc.is_alive():
        proc.terminate()
        proc.join(5)
        raise TimeoutError(f"baostock minute timeout after {timeout_sec}s for {code}")
    if queue.empty():
        raise RuntimeError(f"baostock minute returned no process result for {code}")
    result = queue.get()
    if not result.get("ok"):
        raise RuntimeError(result.get("error") or f"baostock minute failed for {code}")
    return pd.DataFrame(result.get("records") or [])


def validate_frame(df: pd.DataFrame) -> Dict[str, Any]:
    if df.empty:
        return {"rows": 0, "bad_ohlc_rows": 0, "min_datetime": None, "max_datetime": None}
    bad = df[
        (df["open"] <= 0)
        | (df["high"] <= 0)
        | (df["low"] <= 0)
        | (df["close"] <= 0)
        | (df["high"] < df[["open", "low", "close"]].max(axis=1))
        | (df["low"] > df[["open", "high", "close"]].min(axis=1))
    ]
    dup = df.duplicated(["code", "datetime"]).sum()
    return {
        "rows": int(len(df)),
        "codes": int(df["code"].nunique()),
        "min_datetime": str(df["datetime"].min()),
        "max_datetime": str(df["datetime"].max()),
        "bad_ohlc_rows": int(len(bad)),
        "duplicate_rows": int(dup),
    }


def upsert_to_clickhouse(table: str, df: pd.DataFrame, replace: bool) -> int:
    """Insert (or replace) minute data into the target ClickHouse table."""
    if df.empty:
        return 0
    period = table.replace("kline_minute_", "") + "m"
    before_rows = len(df)
    df = filter_trading_day_rows(period, df)
    if df.empty:
        print(f"{table} write blocked by trade_calendar guard: dropped={before_rows}", flush=True)
        return 0
    # kline_minute_* tables use (code, datetime) as natural key for deduplication
    ch = clickhouse_client()
    if replace:
        # Build value tuples for the DELETE clause
        pairs = df[["code", "datetime"]].drop_duplicates()
        for _, row in pairs.iterrows():
            ch.command(
                f"ALTER TABLE {table} DELETE WHERE code = %(code)s AND datetime = %(datetime)s",
                parameters={"code": row["code"], "datetime": row["datetime"]},
            )
    ch.insert_df(table, df)
    return int(len(df))


def main() -> None:
    args = parse_args()
    started = datetime.now()
    codes = [normalize_code(item) for item in args.codes.split(",") if item.strip()]
    target_table = FREQ_TABLE_MAP.get(args.frequency)
    if not target_table:
        raise ValueError(f"Unsupported frequency: {args.frequency}")

    frames: List[pd.DataFrame] = []
    results: List[Dict[str, Any]] = []
    for code in codes:
        try:
            df = fetch_one_timeout(
                code,
                args.start_date,
                args.end_date,
                args.frequency,
                args.adjustflag,
                args.source_tag,
                args.timeout_sec,
            )
            frames.append(df)
            results.append({"code": code, "status": "ok", **validate_frame(df)})
        except Exception as exc:
            results.append({"code": code, "status": "failed",
                            "error": f"{type(exc).__name__}: {exc}"})
        if args.sleep_seconds > 0:
            time.sleep(args.sleep_seconds)

    all_df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    written = 0
    readback: Dict[str, Any] = {}
    if not args.dry_run:
        written = upsert_to_clickhouse(target_table, all_df, replace=args.replace)
        ch = clickhouse_client()
        row = ch.query(
            f"""
            SELECT count(*), count(DISTINCT code), min(datetime), max(datetime)
            FROM {target_table}
            WHERE datetime BETWEEN %(start)s AND %(end)s + INTERVAL 1 DAY
            """,
            parameters={"start": args.start_date, "end": args.end_date},
        ).result_rows[0]
        readback = {
            "rows": int(row[0] or 0),
            "codes": int(row[1] or 0),
            "min_datetime": str(row[2]) if row[2] else None,
            "max_datetime": str(row[3]) if row[3] else None,
        }

    report = {
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "provider": "baostock",
        "table": target_table,
        "frequency": f"{args.frequency}min",
        "adjustflag": args.adjustflag,
        "adjustment": {"1": "hfq", "2": "qfq", "3": "none"}[args.adjustflag],
        "requested_codes": codes,
        "fetched_rows": int(len(all_df)),
        "written_rows": written,
        "validation": validate_frame(all_df),
        "readback": readback,
        "results": results,
    }
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = (
        Path(args.report) if args.report
        else EXPORTS_DIR / f"baostock_minute_{started.strftime('%Y%m%d_%H%M%S')}.json"
    )
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"[report] {report_path}")


if __name__ == "__main__":
    main()

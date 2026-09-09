"""
Collect all baostock-supported A-share minute K-lines into ClickHouse kline_minute_* tables.

ClickHouse-only batch minute collector.
Writes baostock 5/15/30/60 minute bars to corresponding ClickHouse tables.

Usage:
    python scripts/collect_baostock_all_minutes.py
    python scripts/collect_baostock_all_minutes.py --codes 000001.SZ,600000.SH --start-date 2025-01-01
    python scripts/collect_baostock_all_minutes.py --limit-codes 10 --chunk month --no-validate
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import queue as queue_mod
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts.fetch_baostock_minute_to_clickhouse import fetch_one, fetch_one_timeout, normalize_code
from utils.market_warehouse import clickhouse_client
from utils.kline_store import filter_trading_day_rows

EXPORTS_DIR = REPO_ROOT / "data" / "warehouse" / "exports"
STATE_TABLE = "baostock_minute_sync_state"
VALIDATION_TABLE = "baostock_minute_validation"
CODE_WINDOWS: Dict[str, Tuple[str | None, str | None]] = {}
EXPECTED_5M_BARS_CACHE: Dict[Tuple[str, str], int] = {}
BARS_PER_DAY = {
    "5min": 48,
    "15min": 16,
    "30min": 8,
    "60min": 4,
}

# frequency arg → target ClickHouse table
FREQ_TABLE_MAP = {
    "5": "kline_minute_5",
    "15": "kline_minute_15",
    "30": "kline_minute_30",
    "60": "kline_minute_60",
}
# frequency label in state/validation tables
FREQ_LABEL_MAP = {
    "5": "5min",
    "15": "15min",
    "30": "30min",
    "60": "60min",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect all baostock-supported A-share minute K-lines into ClickHouse."
    )
    parser.add_argument("--start-date", default="2016-01-01")
    parser.add_argument("--end-date", default=datetime.now().date().isoformat())
    parser.add_argument("--frequencies", default="5,15,30,60",
                        help="Baostock supports 5,15,30,60 minute bars.")
    parser.add_argument("--codes", default="",
                        help="Optional comma separated code list. Empty means all stocks from the selected code source.")
    parser.add_argument("--codes-file", default="",
                        help="Optional CSV with code, ipoDate, outDate columns. Avoids querying BaoStock basic in each shard.")
    parser.add_argument("--code-source", choices=["local", "baostock-basic"], default="local",
                        help="local=stocks table; baostock-basic=query_stock_basic including inactive stocks.")
    parser.add_argument("--include-bj", action="store_true",
                        help="Include BJ codes even though BaoStock minute API only supports sh/sz stocks.")
    parser.add_argument("--limit-codes", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1,
                        help="Split code list into N deterministic shards.")
    parser.add_argument("--shard-index", type=int, default=0,
                        help="Run only this shard index, 0-based.")
    parser.add_argument("--adjustflag", default="2", choices=["1", "2", "3"],
                        help="1=hfq, 2=qfq, 3=none.")
    parser.add_argument("--source-tag", default="baostock.minute.qfq")
    parser.add_argument("--timeout-sec", type=int, default=90)
    parser.add_argument("--retries", type=int, default=2,
                        help="Retries per BaoStock request after transient failures.")
    parser.add_argument("--sleep-seconds", type=float, default=0.05)
    parser.add_argument("--chunk", choices=["day", "month", "quarter", "year"], default="day")
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--skip-existing-rows", action="store_true",
                        help="Insert only rows whose (code, datetime) key is absent in the target table.")
    parser.add_argument("--skip-complete-5m-days", action="store_true",
                        help="If target 5m table already has at least 48 bars for code/day, avoid BaoStock fetch.")
    parser.add_argument("--skip-complete-5m-chunks", action="store_true",
                        help="If local 5m rows cover every trading day in a chunk, avoid BaoStock fetch and derive higher frequencies locally.")
    parser.add_argument("--resume", action="store_true", default=True)
    parser.add_argument("--no-resume", dest="resume", action="store_false")
    parser.add_argument("--validate", action="store_true", default=True)
    parser.add_argument("--no-validate", dest="validate", action="store_false")
    parser.add_argument("--derive-higher-from-5m", action="store_true",
                        help="Fetch only BaoStock 5m data and derive requested 15/30/60m bars locally.")
    parser.add_argument("--in-process-fetch", action="store_true",
                        help="Use one long-lived BaoStock login in this process instead of spawning one login/logout worker per request.")
    parser.add_argument("--persistent-worker-fetch", action="store_true",
                        help="Use a long-lived BaoStock worker process with parent-side hard timeout and automatic restart on hangs.")
    parser.add_argument("--max-price-diff", type=float, default=0.02)
    parser.add_argument("--max-volume-diff", type=float, default=1.0)
    parser.add_argument("--max-chunks", type=int, default=0,
                        help="Stop after N code/frequency/chunks for smoke runs.")
    parser.add_argument("--report-every", type=int, default=20)
    parser.add_argument("--report", default="")
    return parser.parse_args()


def _ch() -> Any:
    return clickhouse_client()


def ensure_meta_tables() -> None:
    ch = _ch()
    ch.command(
        f"""
        CREATE TABLE IF NOT EXISTS {STATE_TABLE} (
            code String,
            frequency String,
            chunk_start Date,
            chunk_end Date,
            status String,
            rows Int64,
            error String,
            updated_at DateTime
        ) ENGINE = ReplacingMergeTree(updated_at)
        ORDER BY (code, frequency, chunk_start, chunk_end)
        """
    )
    ch.command(
        f"""
        CREATE TABLE IF NOT EXISTS {VALIDATION_TABLE} (
            code String,
            target_frequency String,
            chunk_start Date,
            chunk_end Date,
            compared_rows Int64,
            missing_target_rows Int64,
            max_open_diff Float64,
            max_high_diff Float64,
            max_low_diff Float64,
            max_close_diff Float64,
            max_volume_diff Float64,
            max_amount_diff Float64,
            status String,
            details_json String,
            created_at DateTime
        ) ENGINE = ReplacingMergeTree(created_at)
        ORDER BY (code, target_frequency, chunk_start, chunk_end)
        """
    )


def load_codes(codes_text: str, codes_file: str, limit: int, code_source: str,
               start_date: str, end_date: str) -> List[str]:
    if codes_text.strip():
        codes = [normalize_code(item) for item in codes_text.split(",") if item.strip()]
        return codes[:limit] if limit else codes
    if codes_file.strip():
        return load_codes_from_file(Path(codes_file), limit=limit, start_date=start_date, end_date=end_date)
    if code_source == "baostock-basic":
        return load_codes_from_baostock_basic(limit=limit, start_date=start_date, end_date=end_date)
    ch = _ch()
    suffix = f" LIMIT {int(limit)}" if limit and limit > 0 else ""
    df = ch.query_df(
        "SELECT code FROM stocks WHERE type = 'stock' AND coalesce(quit, 0) = 0 "
        "AND market IN ('SH', 'SZ') ORDER BY code" + suffix
    )
    return [str(item).upper() for item in df["code"].tolist()]


def baostock_to_storage_code(code: str) -> str:
    value = str(code or "").strip()
    if "." in value:
        left, right = value.split(".", 1)
        if left.lower() in {"sh", "sz"}:
            return f"{right.zfill(6).upper()}.{left.upper()}"
    return normalize_code(value)


def filter_baostock_supported_codes(codes: List[str], include_bj: bool) -> List[str]:
    if include_bj:
        return codes
    return [code for code in codes if not str(code).upper().endswith(".BJ")]


def load_codes_from_file(path: Path, limit: int, start_date: str, end_date: str) -> List[str]:
    if not path.exists():
        raise FileNotFoundError(f"codes file not found: {path}")
    df = pd.read_csv(path, dtype=str).fillna("")
    if "code" not in df.columns:
        raise ValueError("codes file must contain a code column")
    if "type" in df.columns:
        df = df[df["type"].astype(str).isin(["1", "stock"])]
    df = df[df["code"].astype(str).str.strip() != ""].copy()
    if "ipoDate" not in df.columns:
        df["ipoDate"] = ""
    if "outDate" not in df.columns:
        df["outDate"] = ""
    df["ipoDate"] = pd.to_datetime(df["ipoDate"].replace("", pd.NA), errors="coerce")
    df["outDate"] = pd.to_datetime(df["outDate"].replace("", pd.NA), errors="coerce")
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)
    df = df[(df["ipoDate"].isna() | (df["ipoDate"] <= end)) & (df["outDate"].isna() | (df["outDate"] >= start))]
    CODE_WINDOWS.clear()
    codes: List[str] = []
    for row in df.itertuples(index=False):
        code = baostock_to_storage_code(getattr(row, "code"))
        ipo = getattr(row, "ipoDate")
        out = getattr(row, "outDate")
        CODE_WINDOWS[code] = (
            None if pd.isna(ipo) else pd.Timestamp(ipo).date().isoformat(),
            None if pd.isna(out) else pd.Timestamp(out).date().isoformat(),
        )
        codes.append(code)
    codes = sorted(set(codes))
    return codes[:limit] if limit and limit > 0 else codes


def load_codes_from_baostock_basic(limit: int, start_date: str, end_date: str) -> List[str]:
    import baostock as bs

    login = bs.login()
    if getattr(login, "error_code", "0") != "0":
        raise RuntimeError(f"baostock login failed: {login.error_code} {login.error_msg}")
    try:
        rs = bs.query_stock_basic()
        rows: List[List[str]] = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        if rs.error_code != "0":
            raise RuntimeError(f"query_stock_basic failed: {rs.error_code} {rs.error_msg}")
    finally:
        bs.logout()
    if not rows:
        return []

    df = pd.DataFrame(rows, columns=rs.fields)
    df = df[(df["type"].astype(str) == "1") & df["code"].astype(str).str.startswith(("sh.", "sz."))].copy()
    df["ipoDate"] = pd.to_datetime(df["ipoDate"], errors="coerce")
    df["outDate"] = pd.to_datetime(df["outDate"].replace("", pd.NA), errors="coerce")
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)
    df = df[(df["ipoDate"].isna() | (df["ipoDate"] <= end)) & (df["outDate"].isna() | (df["outDate"] >= start))]
    codes = [baostock_to_storage_code(item) for item in df["code"].astype(str).tolist()]
    CODE_WINDOWS.clear()
    for row in df.itertuples(index=False):
        code = baostock_to_storage_code(getattr(row, "code"))
        ipo = getattr(row, "ipoDate")
        out = getattr(row, "outDate")
        CODE_WINDOWS[code] = (
            None if pd.isna(ipo) else pd.Timestamp(ipo).date().isoformat(),
            None if pd.isna(out) else pd.Timestamp(out).date().isoformat(),
        )
    codes = sorted(set(codes))
    return codes[:limit] if limit and limit > 0 else codes


def make_chunks(start_date: str, end_date: str, chunk: str) -> List[Tuple[str, str]]:
    start = pd.Timestamp(start_date).date()
    end = pd.Timestamp(end_date).date()
    chunks: List[Tuple[str, str]] = []
    cursor = pd.Timestamp(start)
    while cursor.date() <= end:
        if chunk == "day":
            next_start = cursor + pd.Timedelta(days=1)
        elif chunk == "year":
            next_start = pd.Timestamp(year=cursor.year + 1, month=1, day=1)
        elif chunk == "quarter":
            month = ((cursor.month - 1) // 3) * 3 + 1
            q_start = pd.Timestamp(year=cursor.year, month=month, day=1)
            next_start = q_start + pd.DateOffset(months=3)
            if next_start <= cursor:
                next_start = cursor + pd.DateOffset(months=3)
        else:
            next_start = cursor + pd.DateOffset(months=1)
        chunk_end = min((next_start - pd.Timedelta(days=1)).date(), end)
        chunks.append((cursor.date().isoformat(), chunk_end.isoformat()))
        cursor = pd.Timestamp(chunk_end) + pd.Timedelta(days=1)
    return chunks


def load_trading_day_chunks(start_date: str, end_date: str) -> List[Tuple[str, str]]:
    requested_start = pd.Timestamp(start_date).date()
    requested_end = pd.Timestamp(end_date).date()
    try:
        ch = _ch()
        df = ch.query_df(
            "SELECT DISTINCT trade_date FROM trade_calendar "
            "WHERE is_trading = 1 AND trade_date BETWEEN %(start)s AND %(end)s "
            "ORDER BY trade_date",
            parameters={"start": start_date, "end": end_date},
        )
    except Exception:
        df = pd.DataFrame()
    if df.empty:
        days = [str(item.date()) for item in pd.bdate_range(start=requested_start, end=requested_end)]
        return [(day, day) for day in days]
    known_days = [pd.Timestamp(item).date() for item in df["trade_date"].tolist()]
    days_set = set(known_days)
    min_known = min(known_days)
    max_known = max(known_days)
    if requested_start < min_known:
        days_set.update(item.date() for item in pd.bdate_range(start=requested_start, end=min_known - pd.Timedelta(days=1)))
    if requested_end > max_known:
        days_set.update(item.date() for item in pd.bdate_range(start=max_known + pd.Timedelta(days=1), end=requested_end))
    days = [str(item) for item in sorted(days_set) if requested_start <= item <= requested_end]
    return [(day, day) for day in days]


def is_done(code: str, frequency: str, chunk_start: str, chunk_end: str) -> bool:
    row = _ch().query(
        f"SELECT status FROM {STATE_TABLE} FINAL "
        "WHERE code = %(code)s AND frequency = %(freq)s "
        "AND chunk_start = %(start)s AND chunk_end = %(end)s",
        parameters={"code": code, "freq": frequency, "start": chunk_start, "end": chunk_end},
    ).result_rows
    return bool(row and row[0][0] == "ok")


def load_done_keys(code: str) -> set[Tuple[str, str, str]]:
    df = _ch().query_df(
        f"SELECT frequency, toString(chunk_start) AS chunk_start, toString(chunk_end) AS chunk_end "
        f"FROM {STATE_TABLE} FINAL "
        "WHERE code = %(code)s AND status = 'ok'",
        parameters={"code": code},
    )
    if df.empty:
        return set()
    return {
        (str(row.frequency), str(row.chunk_start)[:10], str(row.chunk_end)[:10])
        for row in df.itertuples(index=False)
    }


def mark_state(code: str, frequency: str, chunk_start: str, chunk_end: str,
               status: str, rows: int, error: str = "") -> None:
    _ch().command(
        f"INSERT INTO {STATE_TABLE} VALUES "
        "(%(code)s, %(freq)s, %(start)s, %(end)s, %(status)s, %(rows)s, %(error)s, now())",
        parameters={
            "code": code, "freq": frequency, "start": chunk_start, "end": chunk_end,
            "status": status, "rows": int(rows), "error": error[:2000],
        },
    )


def _upsert_to_ch(table: str, df: pd.DataFrame, replace: bool) -> int:
    """Insert minute bar data into target ClickHouse kline_minute_* table."""
    if df.empty:
        return 0
    period = table.replace("kline_minute_", "") + "m"
    before_rows = len(df)
    df = filter_trading_day_rows(period, df)
    if df.empty:
        print(f"{table} write blocked by trade_calendar guard: dropped={before_rows}", flush=True)
        return 0
    ch = _ch()
    if replace and not df.empty:
        pairs = df[["code", "datetime"]].drop_duplicates()
        for _, row in pairs.iterrows():
            ch.command(
                f"ALTER TABLE {table} DELETE WHERE code = %(code)s AND datetime = %(datetime)s",
                parameters={"code": row["code"], "datetime": row["datetime"]},
            )
    ch.insert_df(table, df)
    return int(len(df))


def fetch_one_with_retries(
    code: str,
    start_date: str,
    end_date: str,
    frequency: str,
    adjustflag: str,
    source_tag: str,
    timeout_sec: int,
    retries: int,
    sleep_seconds: float,
    in_process_fetch: bool = False,
    worker: Any | None = None,
) -> pd.DataFrame:
    last_error: Exception | None = None
    for attempt in range(max(0, retries) + 1):
        try:
            if worker is not None:
                return worker.fetch(
                    code=code,
                    start_date=start_date,
                    end_date=end_date,
                    frequency=frequency,
                    adjustflag=adjustflag,
                    source_tag=source_tag,
                    timeout_sec=timeout_sec,
                )
            if in_process_fetch:
                return fetch_one(
                    code=code,
                    start_date=start_date,
                    end_date=end_date,
                    frequency=frequency,
                    adjustflag=adjustflag,
                    source_tag=source_tag,
                )
            return fetch_one_timeout(
                code=code,
                start_date=start_date,
                end_date=end_date,
                frequency=frequency,
                adjustflag=adjustflag,
                source_tag=source_tag,
                timeout_sec=timeout_sec,
            )
        except Exception as exc:
            last_error = exc
            if attempt >= max(0, retries):
                break
            time.sleep(max(0.2, sleep_seconds * (attempt + 1) * 3))
    if last_error is not None:
        raise last_error
    return pd.DataFrame()


def _baostock_persistent_worker(requests: mp.Queue, responses: mp.Queue) -> None:
    import baostock as bs

    login = bs.login()
    if getattr(login, "error_code", "0") != "0":
        responses.put({
            "job_id": "__startup__",
            "ok": False,
            "error": f"baostock login failed: {login.error_code} {login.error_msg}",
        })
        return
    responses.put({"job_id": "__startup__", "ok": True})
    try:
        while True:
            job = requests.get()
            if job is None:
                break
            job_id = job["job_id"]
            try:
                df = fetch_one(
                    code=job["code"],
                    start_date=job["start_date"],
                    end_date=job["end_date"],
                    frequency=job["frequency"],
                    adjustflag=job["adjustflag"],
                    source_tag=job["source_tag"],
                )
                responses.put({"job_id": job_id, "ok": True, "records": df.to_dict("records")})
            except Exception as exc:
                responses.put({"job_id": job_id, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
    finally:
        bs.logout()


class BaostockPersistentWorker:
    def __init__(self, startup_timeout_sec: int = 30) -> None:
        self._ctx = mp.get_context("spawn")
        self._requests: mp.Queue | None = None
        self._responses: mp.Queue | None = None
        self._proc: mp.Process | None = None
        self._job_seq = 0
        self._startup_timeout_sec = startup_timeout_sec
        self.start()

    def start(self) -> None:
        self.close(kill=True)
        self._requests = self._ctx.Queue()
        self._responses = self._ctx.Queue()
        self._proc = self._ctx.Process(target=_baostock_persistent_worker, args=(self._requests, self._responses))
        self._proc.start()
        try:
            result = self._responses.get(timeout=self._startup_timeout_sec)
        except queue_mod.Empty as exc:
            self.close(kill=True)
            raise TimeoutError("baostock persistent worker startup timeout") from exc
        if not result.get("ok"):
            self.close(kill=True)
            raise RuntimeError(result.get("error") or "baostock persistent worker startup failed")

    def fetch(
        self,
        code: str,
        start_date: str,
        end_date: str,
        frequency: str,
        adjustflag: str,
        source_tag: str,
        timeout_sec: int,
    ) -> pd.DataFrame:
        if self._proc is None or not self._proc.is_alive():
            self.start()
        assert self._requests is not None
        assert self._responses is not None
        self._job_seq += 1
        job_id = str(self._job_seq)
        self._requests.put({
            "job_id": job_id,
            "code": code,
            "start_date": start_date,
            "end_date": end_date,
            "frequency": frequency,
            "adjustflag": adjustflag,
            "source_tag": source_tag,
        })
        try:
            while True:
                result = self._responses.get(timeout=timeout_sec)
                if result.get("job_id") == job_id:
                    break
        except queue_mod.Empty as exc:
            self.close(kill=True)
            self.start()
            raise TimeoutError(f"baostock persistent worker timeout after {timeout_sec}s for {code}") from exc
        if not result.get("ok"):
            raise RuntimeError(result.get("error") or f"baostock persistent worker failed for {code}")
        return pd.DataFrame(result.get("records") or [])

    def close(self, kill: bool = False) -> None:
        proc = self._proc
        if proc is not None and proc.is_alive():
            if kill:
                proc.terminate()
            else:
                try:
                    if self._requests is not None:
                        self._requests.put(None)
                    proc.join(5)
                except Exception:
                    pass
                if proc.is_alive():
                    proc.terminate()
            proc.join(5)
        self._proc = None
        self._requests = None
        self._responses = None


def filter_missing_rows(table: str, df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    ch = _ch()
    code = str(df["code"].iloc[0])
    min_dt = df["datetime"].min()
    max_dt = df["datetime"].max()
    existing = ch.query_df(
        f"SELECT datetime FROM {table} "
        "WHERE code = %(code)s AND datetime >= %(min_dt)s AND datetime <= %(max_dt)s",
        parameters={"code": code, "min_dt": min_dt, "max_dt": max_dt},
    )
    if existing.empty:
        return df
    existing_keys = set(pd.to_datetime(existing["datetime"]).tolist())
    work = df.copy()
    work["_dt_key"] = pd.to_datetime(work["datetime"])
    work = work[~work["_dt_key"].isin(existing_keys)].drop(columns=["_dt_key"])
    return work


def has_complete_5m_day(code: str, day: str) -> bool:
    row = _ch().query(
        "SELECT count() FROM kline_minute_5 "
        "WHERE code = %(code)s AND datetime >= toDateTime(%(day)s) "
        "AND datetime < toDateTime(%(day)s) + INTERVAL 1 DAY",
        parameters={"code": code, "day": day},
    ).result_rows
    return bool(row and int(row[0][0] or 0) >= 48)


def expected_5m_bars(start_date: str, end_date: str) -> int:
    cache_key = (start_date, end_date)
    if cache_key in EXPECTED_5M_BARS_CACHE:
        return EXPECTED_5M_BARS_CACHE[cache_key]
    row = _ch().query(
        "SELECT count() FROM trade_calendar "
        "WHERE trade_date >= toDate(%(start)s) AND trade_date <= toDate(%(end)s) "
        "AND is_trading = 1",
        parameters={"start": start_date, "end": end_date},
    ).result_rows
    trading_days = int(row[0][0] or 0) if row else 0
    expected = trading_days * 48
    EXPECTED_5M_BARS_CACHE[cache_key] = expected
    return expected


def expected_bars(frequency: str, start_date: str, end_date: str) -> int:
    expected_5m = expected_5m_bars(start_date, end_date)
    per_day = BARS_PER_DAY.get(frequency)
    if not per_day:
        return 0
    return expected_5m // 48 * per_day


def has_complete_5m_chunk(code: str, start_date: str, end_date: str) -> bool:
    expected = expected_5m_bars(start_date, end_date)
    if expected <= 0:
        return False
    row = _ch().query(
        "SELECT count() FROM kline_minute_5 "
        "WHERE code = %(code)s AND datetime >= toDateTime(%(start)s) "
        "AND datetime < toDateTime(%(end)s) + INTERVAL 1 DAY",
        parameters={"code": code, "start": start_date, "end": end_date},
    ).result_rows
    actual = int(row[0][0] or 0) if row else 0
    return actual >= expected


def load_chunk_counts(table: str, code: str, chunks: List[Tuple[str, str]]) -> Dict[Tuple[str, str], int]:
    if not chunks:
        return {}
    range_start = min(item[0] for item in chunks)
    range_end = max(item[1] for item in chunks)
    if table not in FREQ_TABLE_MAP.values():
        raise ValueError(f"Unsupported minute table: {table}")
    df = _ch().query_df(
        "SELECT toString(toDate(toStartOfMonth(datetime))) AS chunk_start, count() AS rows "
        f"FROM {table} "
        "WHERE code = %(code)s AND datetime >= toDateTime(%(start)s) "
        "AND datetime < toDateTime(%(end)s) + INTERVAL 1 DAY "
        "GROUP BY chunk_start",
        parameters={"code": code, "start": range_start, "end": range_end},
    )
    if df.empty:
        return {}
    by_month = {str(row.chunk_start)[:10]: int(row.rows or 0) for row in df.itertuples(index=False)}
    return {
        (chunk_start, chunk_end): int(by_month.get(chunk_start[:10], 0))
        for chunk_start, chunk_end in chunks
    }


def load_frequency_chunk_counts(code: str, frequencies: List[str],
                                chunks: List[Tuple[str, str]]) -> Dict[str, Dict[Tuple[str, str], int]]:
    result: Dict[str, Dict[Tuple[str, str], int]] = {}
    for frequency in frequencies:
        minutes = frequency.replace("min", "")
        table = FREQ_TABLE_MAP.get(minutes)
        if table:
            result[frequency] = load_chunk_counts(table, code, chunks)
    return result


def has_complete_5m_chunk_count(chunk_counts: Dict[Tuple[str, str], int],
                                chunk_start: str, chunk_end: str) -> bool:
    expected = expected_5m_bars(chunk_start, chunk_end)
    if expected <= 0:
        return False
    return int(chunk_counts.get((chunk_start, chunk_end), 0)) >= expected


def has_complete_frequency_chunk_count(chunk_counts: Dict[Tuple[str, str], int],
                                       frequency: str, chunk_start: str, chunk_end: str) -> bool:
    expected = expected_bars(frequency, chunk_start, chunk_end)
    if expected <= 0:
        return False
    return int(chunk_counts.get((chunk_start, chunk_end), 0)) >= expected


def load_chunk(table: str, code: str, start_date: str, end_date: str) -> pd.DataFrame:
    ch = _ch()
    if table not in FREQ_TABLE_MAP.values():
        raise ValueError(f"Unsupported minute table: {table}")
    return ch.query_df(
        "SELECT datetime, open, high, low, close, volume, amount "
        f"FROM {table} "
        "WHERE code = %(code)s AND datetime BETWEEN %(start)s AND %(end)s + INTERVAL 1 DAY "
        "ORDER BY datetime",
        parameters={"code": code, "start": start_date, "end": end_date},
    )


def aggregate_from_5m(base: pd.DataFrame, target_minutes: int) -> pd.DataFrame:
    if base.empty:
        return pd.DataFrame()
    group_size = target_minutes // 5
    work = base.sort_values("datetime").copy()
    work["_day"] = pd.to_datetime(work["datetime"]).dt.date
    work["_grp"] = work.groupby("_day").cumcount() // group_size
    grouped = work.groupby(["_day", "_grp"], as_index=False)
    return grouped.agg(
        datetime=("datetime", "max"),
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        amount=("amount", "sum"),
    )


def derive_higher_frame(df_5m: pd.DataFrame, code: str, target_minutes: int) -> pd.DataFrame:
    if df_5m.empty:
        return pd.DataFrame()
    result = aggregate_from_5m(df_5m, target_minutes)
    if result.empty:
        return pd.DataFrame()
    result["code"] = code
    result["created_at"] = datetime.now()
    result["id"] = [
        abs(hash((row.code, int(pd.Timestamp(row.datetime).timestamp()), f"{target_minutes}m", "baostock.derived5m"))) % (2**63)
        for row in result.itertuples(index=False)
    ]
    return result[["code", "datetime", "open", "high", "low", "close", "volume", "amount", "created_at", "id"]]


def validate_chunk(
    code: str,
    target_frequency: str,
    chunk_start: str,
    chunk_end: str,
    source: str,
    max_price_diff: float,
    max_volume_diff: float,
) -> Dict[str, Any]:
    target_minutes = int(target_frequency.replace("min", ""))
    base_table = FREQ_TABLE_MAP["5"]
    target_table = FREQ_TABLE_MAP.get(str(target_minutes))
    if not target_table:
        raise ValueError(f"Unsupported target frequency: {target_frequency}")
    base = load_chunk(base_table, code, chunk_start, chunk_end)
    target = load_chunk(target_table, code, chunk_start, chunk_end)
    agg = aggregate_from_5m(base, target_minutes)
    result: Dict[str, Any] = {
        "code": code,
        "target_frequency": target_frequency,
        "chunk_start": chunk_start,
        "chunk_end": chunk_end,
        "base_rows": int(len(base)),
        "target_rows": int(len(target)),
        "compared_rows": 0,
        "missing_target_rows": 0,
        "status": "no_data",
    }
    if agg.empty or target.empty:
        _write_validation(result, max_price_diff, max_volume_diff)
        return result
    merged = agg.merge(target, on="datetime", suffixes=("_agg5", "_target"))
    result["compared_rows"] = int(len(merged))
    result["missing_target_rows"] = int(max(0, len(agg) - len(merged)))
    diffs: Dict[str, float] = {}
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        diffs[col] = float((merged[f"{col}_agg5"] - merged[f"{col}_target"]).abs().max()) if not merged.empty else 0.0
    result.update({f"max_{key}_diff": value for key, value in diffs.items()})
    bad = (
        diffs.get("open", 0.0) > max_price_diff
        or diffs.get("high", 0.0) > max_price_diff
        or diffs.get("low", 0.0) > max_price_diff
        or diffs.get("close", 0.0) > max_price_diff
        or diffs.get("volume", 0.0) > max_volume_diff
    )
    result["status"] = "failed" if bad or result["missing_target_rows"] else "ok"
    _write_validation(result, max_price_diff, max_volume_diff)
    return result


def _write_validation(result: Dict[str, Any], max_price_diff: float, max_volume_diff: float) -> None:
    ch = _ch()
    ch.command(
        f"INSERT INTO {VALIDATION_TABLE} VALUES "
        "(%(code)s, %(freq)s, %(start)s, %(end)s, %(compared)s, %(missing)s, "
        "%(open_diff)s, %(high_diff)s, %(low_diff)s, %(close_diff)s, "
        "%(volume_diff)s, %(amount_diff)s, %(status)s, %(details)s, now())",
        parameters={
            "code": result["code"],
            "freq": result["target_frequency"],
            "start": result["chunk_start"],
            "end": result["chunk_end"],
            "compared": result["compared_rows"],
            "missing": result["missing_target_rows"],
            "open_diff": float(result.get("max_open_diff", 0.0)),
            "high_diff": float(result.get("max_high_diff", 0.0)),
            "low_diff": float(result.get("max_low_diff", 0.0)),
            "close_diff": float(result.get("max_close_diff", 0.0)),
            "volume_diff": float(result.get("max_volume_diff", 0.0)),
            "amount_diff": float(result.get("max_amount_diff", 0.0)),
            "status": result["status"],
            "details": json.dumps(result, ensure_ascii=False),
        },
    )


def summarize(source: str) -> Dict[str, Any]:
    ch = _ch()
    # Aggregate across all kline_minute_* tables
    total_rows = 0
    all_codes: set = set()
    all_min: str | None = None
    all_max: str | None = None
    for tbl in FREQ_TABLE_MAP.values():
        try:
            row = ch.query(
                "SELECT count(*), count(DISTINCT code), min(datetime), max(datetime) "
                f"FROM {tbl}"
            ).result_rows[0]
            r = int(row[0] or 0)
            total_rows += r
            for c in ch.query_df(f"SELECT DISTINCT code FROM {tbl}")["code"].tolist():
                all_codes.add(str(c))
            if row[2] and (all_min is None or str(row[2]) < all_min):
                all_min = str(row[2])
            if row[3] and (all_max is None or str(row[3]) > all_max):
                all_max = str(row[3])
        except Exception:
            pass
    states_df = ch.query_df(
        f"SELECT status, count(*) AS chunks, sum(rows) AS rows "
        f"FROM {STATE_TABLE} FINAL GROUP BY status ORDER BY status"
    )
    validations_df = ch.query_df(
        f"SELECT status, target_frequency, count(*) AS chunks "
        f"FROM {VALIDATION_TABLE} FINAL GROUP BY status, target_frequency "
        f"ORDER BY target_frequency, status"
    )
    return {
        "rows": total_rows,
        "codes": len(all_codes),
        "min_datetime": all_min,
        "max_datetime": all_max,
        "states": states_df.to_dict("records") if not states_df.empty else [],
        "validations": validations_df.to_dict("records") if not validations_df.empty else [],
    }


def main() -> None:
    args = parse_args()
    started = datetime.now()
    frequencies = [item.strip() for item in args.frequencies.split(",") if item.strip()]
    frequencies = [item if item.endswith("min") else f"{item}min" for item in frequencies]
    fetch_frequencies = [item.replace("min", "") for item in frequencies]
    if args.derive_higher_from_5m and "5min" not in frequencies:
        raise ValueError("--derive-higher-from-5m requires 5min in --frequencies")
    if args.in_process_fetch and args.persistent_worker_fetch:
        raise ValueError("--in-process-fetch and --persistent-worker-fetch cannot be used together")
    chunks = make_chunks(args.start_date, args.end_date, args.chunk)

    ensure_meta_tables()
    codes = load_codes(args.codes, args.codes_file, args.limit_codes, args.code_source, args.start_date, args.end_date)
    codes = filter_baostock_supported_codes(codes, include_bj=args.include_bj)
    if args.shard_count < 1:
        raise ValueError("--shard-count must be >= 1")
    if args.shard_index < 0 or args.shard_index >= args.shard_count:
        raise ValueError("--shard-index must be in [0, shard-count)")
    if args.shard_count > 1:
        codes = [code for idx, code in enumerate(codes) if idx % args.shard_count == args.shard_index]

    if args.chunk == "day":
        chunks = load_trading_day_chunks(args.start_date, args.end_date)

    print(json.dumps({
        "started_at": started.isoformat(timespec="seconds"),
        "codes": len(codes),
        "shard_index": args.shard_index,
        "shard_count": args.shard_count,
        "frequencies": frequencies,
        "chunks": len(chunks),
        "source": args.source_tag,
    }, ensure_ascii=False))

    processed = 0
    fetched_rows = 0
    failures: List[Dict[str, Any]] = []
    validation_failures: List[Dict[str, Any]] = []

    bs_session = None
    bs_worker = BaostockPersistentWorker(startup_timeout_sec=max(15, min(args.timeout_sec, 60))) if args.persistent_worker_fetch else None
    if args.in_process_fetch:
        import baostock as bs

        login = bs.login()
        if getattr(login, "error_code", "0") != "0":
            raise RuntimeError(f"baostock login failed: {login.error_code} {login.error_msg}")
        bs_session = bs
    try:
        for code in codes:
            done_keys = load_done_keys(code) if args.resume else set()
            chunk_counts_by_frequency = (
                load_frequency_chunk_counts(code, frequencies, chunks)
                if args.skip_complete_5m_chunks else {}
            )
            chunk_counts_5m = chunk_counts_by_frequency.get("5min", {})
            for chunk_start, chunk_end in chunks:
                window = CODE_WINDOWS.get(code)
                if window:
                    ipo_date, out_date = window
                    if ipo_date and chunk_end < ipo_date:
                        continue
                    if out_date and chunk_start > out_date:
                        continue
                if args.derive_higher_from_5m and args.resume and all(
                    (frequency, chunk_start, chunk_end) in done_keys for frequency in frequencies
                ):
                    processed += 1
                    if args.report_every and processed % args.report_every == 0:
                        print(f"[progress] chunks={processed} fetched_rows={fetched_rows} "
                              f"failures={len(failures)} code={code} chunk={chunk_start}~{chunk_end}")
                    continue
                if args.derive_higher_from_5m and args.skip_complete_5m_chunks and all(
                    has_complete_frequency_chunk_count(
                        chunk_counts_by_frequency.get(frequency, {}),
                        frequency,
                        chunk_start,
                        chunk_end,
                    )
                    for frequency in frequencies
                ):
                    for frequency in frequencies:
                        if args.resume and (frequency, chunk_start, chunk_end) in done_keys:
                            continue
                        mark_state(code, frequency, chunk_start, chunk_end, "ok", 0)
                        done_keys.add((frequency, chunk_start, chunk_end))
                    processed += 1
                    if args.report_every and processed % args.report_every == 0:
                        print(f"[progress] chunks={processed} fetched_rows={fetched_rows} "
                              f"failures={len(failures)} code={code} chunk={chunk_start}~{chunk_end}")
                    continue
                chunk_freq_ok: Dict[str, bool] = {}
                if args.derive_higher_from_5m:
                    frequency = "5min"
                    if args.resume and (frequency, chunk_start, chunk_end) in done_keys:
                        chunk_freq_ok[frequency] = True
                        processed += 1
                        df_5m = load_chunk(FREQ_TABLE_MAP["5"], code, chunk_start, chunk_end)
                    elif args.skip_complete_5m_days and chunk_start == chunk_end and has_complete_5m_day(code, chunk_start):
                        df_5m = load_chunk(FREQ_TABLE_MAP["5"], code, chunk_start, chunk_end)
                        mark_state(code, frequency, chunk_start, chunk_end, "ok", 0)
                        done_keys.add((frequency, chunk_start, chunk_end))
                        chunk_freq_ok[frequency] = True
                        processed += 1
                    elif args.skip_complete_5m_chunks and has_complete_5m_chunk_count(chunk_counts_5m, chunk_start, chunk_end):
                        df_5m = load_chunk(FREQ_TABLE_MAP["5"], code, chunk_start, chunk_end)
                        mark_state(code, frequency, chunk_start, chunk_end, "ok", 0)
                        done_keys.add((frequency, chunk_start, chunk_end))
                        chunk_freq_ok[frequency] = True
                        processed += 1
                    else:
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
                                worker=bs_worker,
                            )
                            df_5m_to_write = filter_missing_rows(FREQ_TABLE_MAP["5"], df_5m) if args.skip_existing_rows else df_5m
                            written = _upsert_to_ch(FREQ_TABLE_MAP["5"], df_5m_to_write, replace=args.replace)
                            mark_state(code, frequency, chunk_start, chunk_end, "ok", int(written))
                            done_keys.add((frequency, chunk_start, chunk_end))
                            fetched_rows += int(written)
                            chunk_freq_ok[frequency] = True
                        except Exception as exc:
                            error = f"{type(exc).__name__}: {exc}"
                            failures.append({
                                "code": code, "frequency": frequency,
                                "chunk_start": chunk_start, "chunk_end": chunk_end, "error": error,
                            })
                            mark_state(code, frequency, chunk_start, chunk_end, "failed", 0, error)
                            chunk_freq_ok[frequency] = False
                            df_5m = pd.DataFrame()
                        processed += 1
                        if args.sleep_seconds > 0:
                            time.sleep(args.sleep_seconds)
                        if args.report_every and processed % args.report_every == 0:
                            print(f"[progress] chunks={processed} fetched_rows={fetched_rows} "
                                  f"failures={len(failures)} code={code} chunk={chunk_start}~{chunk_end}")
                        if args.max_chunks and processed >= args.max_chunks:
                            break

                    if chunk_freq_ok.get("5min"):
                        for frequency in frequencies:
                            if frequency == "5min":
                                continue
                            target_minutes = int(frequency.replace("min", ""))
                            if args.resume and (frequency, chunk_start, chunk_end) in done_keys:
                                chunk_freq_ok[frequency] = True
                                continue
                            try:
                                df_target = derive_higher_frame(df_5m, code, target_minutes)
                                if args.skip_existing_rows:
                                    df_target = filter_missing_rows(FREQ_TABLE_MAP[str(target_minutes)], df_target)
                                written = _upsert_to_ch(FREQ_TABLE_MAP[str(target_minutes)], df_target, replace=args.replace)
                                mark_state(code, frequency, chunk_start, chunk_end, "ok", int(written))
                                done_keys.add((frequency, chunk_start, chunk_end))
                                fetched_rows += int(written)
                                chunk_freq_ok[frequency] = True
                            except Exception as exc:
                                error = f"{type(exc).__name__}: {exc}"
                                failures.append({
                                    "code": code, "frequency": frequency,
                                    "chunk_start": chunk_start, "chunk_end": chunk_end, "error": error,
                                })
                                mark_state(code, frequency, chunk_start, chunk_end, "failed", 0, error)
                                chunk_freq_ok[frequency] = False
                    if args.max_chunks and processed >= args.max_chunks:
                        break
                else:
                    for fetch_frequency, frequency in zip(fetch_frequencies, frequencies):
                        if args.resume and (frequency, chunk_start, chunk_end) in done_keys:
                            chunk_freq_ok[frequency] = True
                            continue
                        target_table = FREQ_TABLE_MAP.get(fetch_frequency)
                        if not target_table:
                            continue
                        try:
                            df = fetch_one_with_retries(
                                code=code,
                                start_date=chunk_start,
                                end_date=chunk_end,
                                frequency=fetch_frequency,
                                adjustflag=args.adjustflag,
                                source_tag=args.source_tag,
                                timeout_sec=args.timeout_sec,
                                retries=args.retries,
                                sleep_seconds=args.sleep_seconds,
                                in_process_fetch=args.in_process_fetch,
                                worker=bs_worker,
                            )
                            # df already has columns: code, datetime, open, high, low, close, volume, amount, created_at
                            if args.skip_existing_rows:
                                df = filter_missing_rows(target_table, df)
                            written = _upsert_to_ch(target_table, df, replace=args.replace)
                            mark_state(code, frequency, chunk_start, chunk_end, "ok", int(written))
                            done_keys.add((frequency, chunk_start, chunk_end))
                            fetched_rows += int(written)
                            chunk_freq_ok[frequency] = True
                        except Exception as exc:
                            error = f"{type(exc).__name__}: {exc}"
                            failures.append({
                                "code": code, "frequency": frequency,
                                "chunk_start": chunk_start, "chunk_end": chunk_end, "error": error,
                            })
                            mark_state(code, frequency, chunk_start, chunk_end, "failed", 0, error)
                            chunk_freq_ok[frequency] = False
                        processed += 1
                        if args.sleep_seconds > 0:
                            time.sleep(args.sleep_seconds)
                        if args.report_every and processed % args.report_every == 0:
                            print(f"[progress] chunks={processed} fetched_rows={fetched_rows} "
                                  f"failures={len(failures)} code={code} chunk={chunk_start}~{chunk_end}")
                        if args.max_chunks and processed >= args.max_chunks:
                            break
                if args.validate and chunk_freq_ok.get("5min"):
                    for target_freq_label in ["15min", "30min", "60min"]:
                        if target_freq_label in frequencies and chunk_freq_ok.get(target_freq_label):
                            result = validate_chunk(
                                code, target_freq_label,
                                chunk_start, chunk_end, args.source_tag,
                                args.max_price_diff, args.max_volume_diff,
                            )
                            if result.get("status") not in {"ok", "no_data"}:
                                validation_failures.append(result)
                if args.max_chunks and processed >= args.max_chunks:
                    break
            if args.max_chunks and processed >= args.max_chunks:
                break
    finally:
        if bs_session is not None:
            bs_session.logout()
        if bs_worker is not None:
            bs_worker.close()

    summary = summarize(args.source_tag)
    report = {
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "codes_requested": len(codes),
        "frequencies": frequencies,
        "chunks_per_code": len(chunks),
        "processed_chunks": processed,
        "fetched_rows": fetched_rows,
        "failure_count": len(failures),
        "validation_failure_count": len(validation_failures),
        "failure_samples": failures[:20],
        "validation_failure_samples": validation_failures[:20],
        "summary": summary,
    }
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = (
        Path(args.report) if args.report
        else EXPORTS_DIR / f"baostock_all_minutes_{started.strftime('%Y%m%d_%H%M%S')}.json"
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"[report] {report_path}")


if __name__ == "__main__":
    main()

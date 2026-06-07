"""Backfill BJ/920 5m minute bars from pytdx and derive 15/30/60m bars.

BaoStock does not support BJ minute bars.  This script uses the public TDX
HQ protocol through pytdx, then aligns raw 5m prices to the local daily qfq
close before writing the standard ClickHouse minute tables.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.collect_baostock_all_minutes import (  # noqa: E402
    FREQ_TABLE_MAP,
    _upsert_to_ch,
    derive_higher_frame,
    filter_missing_rows,
)
from utils.market_warehouse import clickhouse_client  # noqa: E402


DEFAULT_HOSTS = [
    "180.153.18.170:7709",
    "123.125.108.90:7709",
    "47.103.48.45:7709",
    "119.147.212.81:7709",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill BJ 5m minute bars from pytdx.")
    parser.add_argument("--codes", default="", help="Comma-separated full BJ codes, e.g. 920000.BJ.")
    parser.add_argument("--codes-file", default="", help="CSV or text file containing a code/daily.code column.")
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--hosts", default=",".join(DEFAULT_HOSTS))
    parser.add_argument("--start-offset", type=int, default=16000)
    parser.add_argument("--max-offset", type=int, default=24800)
    parser.add_argument("--page-size", type=int, default=800)
    parser.add_argument("--sleep-seconds", type=float, default=0.05)
    parser.add_argument("--limit-codes", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report-every", type=int, default=10)
    parser.add_argument("--report", default="")
    return parser.parse_args()


def _normalize_code(value: str) -> str:
    code = str(value or "").strip().upper()
    if not code:
        return ""
    if "." in code:
        left, right = code.split(".", 1)
        if right == "BJ":
            return f"{left.zfill(6)}.BJ"
        if left == "BJ":
            return f"{right.zfill(6)}.BJ"
        return code
    return f"{code.zfill(6)}.BJ"


def _load_codes(args: argparse.Namespace) -> list[str]:
    codes: list[str] = []
    if args.codes.strip():
        codes.extend(_normalize_code(item) for item in args.codes.split(","))
    if args.codes_file:
        path = Path(args.codes_file)
        if not path.exists():
            raise FileNotFoundError(path)
        if path.suffix.lower() == ".csv":
            df = pd.read_csv(path, dtype=str).fillna("")
            code_col = "code" if "code" in df.columns else "daily.code" if "daily.code" in df.columns else None
            if code_col is None:
                raise ValueError("codes CSV must contain code or daily.code column")
            codes.extend(_normalize_code(item) for item in df[code_col].tolist())
        else:
            codes.extend(_normalize_code(line) for line in path.read_text(encoding="utf-8").splitlines())
    codes = sorted({code for code in codes if code.endswith(".BJ")})
    if args.limit_codes and args.limit_codes > 0:
        codes = codes[: args.limit_codes]
    return codes


def _load_missing_bj_codes(start_date: str, end_date: str, limit: int) -> list[str]:
    ch = clickhouse_client()
    suffix = f" LIMIT {int(limit)}" if limit and limit > 0 else ""
    df = ch.query_df(
        """
        WITH daily AS (
            SELECT d.code, d.trade_date
            FROM kline_daily d INNER JOIN stocks s ON d.code=s.code
            WHERE s.type='stock' AND s.market='BJ'
              AND d.trade_date >= toDate(%(start)s)
              AND d.trade_date <= toDate(%(end)s)
            GROUP BY d.code, d.trade_date
        ), m AS (
            SELECT code, toDate(datetime) trade_date, count() bars
            FROM kline_minute_5
            WHERE datetime >= toDateTime(%(start)s)
              AND datetime < toDateTime(%(end)s) + INTERVAL 1 DAY
            GROUP BY code, trade_date
        )
        SELECT daily.code AS code
        FROM daily LEFT JOIN m USING(code, trade_date)
        GROUP BY daily.code
        HAVING countIf(coalesce(m.bars,0)=0) > 0
            OR countIf(coalesce(m.bars,0)>0 AND coalesce(m.bars,0)<48) > 0
        ORDER BY daily.code
        """
        + suffix,
        parameters={"start": start_date, "end": end_date},
    )
    return [str(item).upper() for item in df["code"].tolist()]


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


def _stable_id(code: str, dt: datetime, period: str) -> int:
    key = f"{code}|{dt:%Y-%m-%d %H:%M:%S}|{period}|pytdx.bj.qfq".encode("utf-8")
    return int.from_bytes(hashlib.blake2b(key, digest_size=8).digest(), "big", signed=False) % (2**63)


def _daily_qfq_close(code: str, days: Iterable[pd.Timestamp]) -> dict[pd.Timestamp, float]:
    day_values = sorted({pd.Timestamp(day).date() for day in days})
    if not day_values:
        return {}
    rows = clickhouse_client().query(
        """
        SELECT trade_date, close
        FROM kline_daily
        WHERE code = %(code)s AND trade_date >= %(start)s AND trade_date <= %(end)s
        """,
        parameters={"code": code, "start": day_values[0], "end": day_values[-1]},
    ).result_rows
    return {pd.Timestamp(row[0]).date(): float(row[1]) for row in rows if row[1] and float(row[1]) > 0}


def _apply_qfq(code: str, df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    if df.empty:
        return df, 0
    work = df.copy()
    work["_day"] = pd.to_datetime(work["datetime"]).dt.date
    daily_close = _daily_qfq_close(code, work["_day"].tolist())
    missing = 0
    for day, idx in work.groupby("_day").groups.items():
        raw_close = float(work.loc[list(idx), "close"].iloc[-1] or 0)
        qfq_close = daily_close.get(day)
        if qfq_close and raw_close > 0:
            factor = qfq_close / raw_close
        else:
            factor = 1.0
            missing += 1
        for col in ("open", "high", "low", "close"):
            work.loc[list(idx), col] = (work.loc[list(idx), col].astype(float) * factor).round(4)
    return work.drop(columns=["_day"]), missing


def _fetch_5m(api: Any, code: str, args: argparse.Namespace) -> pd.DataFrame:
    raw_code = code.split(".", 1)[0]
    start_ts = pd.Timestamp(args.start_date)
    end_ts = pd.Timestamp(args.end_date) + pd.Timedelta(days=1)
    frames: list[pd.DataFrame] = []
    for offset in range(args.start_offset, args.max_offset + 1, args.page_size):
        bars = api.get_security_bars(0, 2, raw_code, offset, args.page_size) or []
        if not bars:
            continue
        df = pd.DataFrame(bars)
        if df.empty or "datetime" not in df.columns:
            continue
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        df = df[(df["datetime"] >= start_ts) & (df["datetime"] < end_ts)].copy()
        if not df.empty:
            frames.append(df)
        if args.sleep_seconds > 0:
            time.sleep(args.sleep_seconds)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True).drop_duplicates(["datetime"]).sort_values("datetime")
    out = out.rename(columns={"vol": "volume"})
    for col in ("open", "high", "low", "close", "volume", "amount"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["datetime", "open", "high", "low", "close"])
    if out.empty:
        return pd.DataFrame()
    out["code"] = code
    out["created_at"] = datetime.now()
    out["id"] = [_stable_id(code, pd.Timestamp(dt).to_pydatetime(), "5m") for dt in out["datetime"]]
    return out[["code", "datetime", "open", "high", "low", "close", "volume", "amount", "created_at", "id"]]


def _write_one(code: str, df_5m: pd.DataFrame, dry_run: bool) -> dict[str, int]:
    written = {"5m": 0, "15m": 0, "30m": 0, "60m": 0}
    if df_5m.empty:
        return written
    df_5m = filter_missing_rows(FREQ_TABLE_MAP["5"], df_5m)
    if not dry_run:
        written["5m"] = _upsert_to_ch(FREQ_TABLE_MAP["5"], df_5m, replace=False)
    else:
        written["5m"] = int(len(df_5m))
    for minutes in (15, 30, 60):
        df_target = derive_higher_frame(df_5m, code, minutes)
        df_target = filter_missing_rows(FREQ_TABLE_MAP[str(minutes)], df_target)
        key = f"{minutes}m"
        if not dry_run:
            written[key] = _upsert_to_ch(FREQ_TABLE_MAP[str(minutes)], df_target, replace=False)
        else:
            written[key] = int(len(df_target))
    return written


def main() -> int:
    args = parse_args()
    codes = _load_codes(args)
    if not codes:
        codes = _load_missing_bj_codes(args.start_date, args.end_date, args.limit_codes)
    hosts = _parse_hosts(args.hosts)
    if not codes:
        raise SystemExit("No BJ codes to process")
    api = _connect(hosts)
    started = datetime.now()
    summary = {
        "started_at": started.isoformat(timespec="seconds"),
        "start_date": args.start_date,
        "end_date": args.end_date,
        "codes": len(codes),
        "processed": 0,
        "source_5m_rows": 0,
        "qfq_missing_factor_days": 0,
        "written": {"5m": 0, "15m": 0, "30m": 0, "60m": 0},
        "failures": [],
        "dry_run": bool(args.dry_run),
    }
    try:
        for idx, code in enumerate(codes, start=1):
            try:
                df_5m = _fetch_5m(api, code, args)
                summary["source_5m_rows"] += int(len(df_5m))
                df_5m, missing_factor_days = _apply_qfq(code, df_5m)
                summary["qfq_missing_factor_days"] += int(missing_factor_days)
                written = _write_one(code, df_5m, args.dry_run)
                for key, value in written.items():
                    summary["written"][key] += int(value)
                summary["processed"] += 1
                if args.report_every and idx % args.report_every == 0:
                    print(json.dumps({"progress": idx, "code": code, "written": summary["written"]}, ensure_ascii=False), flush=True)
            except Exception as exc:
                summary["failures"].append({"code": code, "error": f"{type(exc).__name__}: {exc}"})
    finally:
        try:
            api.disconnect()
        except Exception:
            pass
    summary["finished_at"] = datetime.now().isoformat(timespec="seconds")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    if args.report:
        path = Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if not summary["failures"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

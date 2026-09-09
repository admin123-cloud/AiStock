from __future__ import annotations

"""Low-level QMT minute worker.

Top-level diagnostics and scheduled repair should call
``qmt_xtquant_minute_gap_audit_repair.py``. This module remains as the
single-batch fetch/validate/apply worker used by that safer wrapper.
"""

import argparse
import json
import multiprocessing as mp
import subprocess
import sys
import time
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from data_fetcher.sources.qmtmini_client import QmtMiniMarketClient
from scripts.qmtmini_daily_backfill_validate import ch_client, insert_request_log, load_codes, quote_sql
from utils.paths import report_path
from utils.kline_units import normalize_qmt_minute_values, MINUTE_UNIT_CONTRACT_ID
from utils.qmt_universe import qmt_universe_filter_sql


FIELD_LIST = ["open", "high", "low", "close", "volume", "amount"]
TARGET_COLUMNS = ["period", "code", "datetime", "open", "high", "low", "close", "volume", "amount", "created_at", "id"]
PERIOD_TO_MAIN_TABLE = {
    "1m": "kline_minute_1",
    "5m": "kline_minute_5",
    "15m": "kline_minute_15",
    "30m": "kline_minute_30",
    "60m": "kline_minute_60",
}
DEFAULT_STAGE_TABLE = "qmt_xtquant_minute_stage"
DEFAULT_LATEST_5M_TABLE = "qmt_intraday_latest_5m"
DEFAULT_INDEX_CODES = "000001.SH,399001.SZ,399006.SZ,000300.SH,000905.SH,000852.SH"
BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
REGULAR_ASHARE_5M_TIMES = {
    *(dt_time(9, minute) for minute in range(35, 60, 5)),
    *(dt_time(hour, minute) for hour in (10,) for minute in range(0, 60, 5)),
    *(dt_time(11, minute) for minute in range(0, 35, 5)),
    *(dt_time(13, minute) for minute in range(5, 60, 5)),
    *(dt_time(14, minute) for minute in range(0, 60, 5)),
    *(dt_time(15, minute) for minute in (0,)),
}


def _regular_bar_times(period: str) -> set[dt_time]:
    """Return exchange close timestamps persisted for a regular A-share session."""
    minutes = {"5m": 5, "15m": 15, "30m": 30, "60m": 60}.get(period)
    if not minutes:
        return set()
    out: set[dt_time] = set()
    for start, end in ((9 * 60 + 30, 11 * 60 + 30), (13 * 60, 15 * 60)):
        current = start + minutes
        while current <= end:
            out.add(dt_time(current // 60, current % 60))
            current += minutes
    return out


def _regular_bar_time_sql(period: str, column: str = "datetime") -> str:
    values = sorted(_regular_bar_times(period))
    if not values:
        return "1"
    encoded = ",".join(f"({item.hour},{item.minute})" for item in values)
    return f"(toHour({column}), toMinute({column})) IN ({encoded})"


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def chunked(items: list[str], size: int) -> Iterable[list[str]]:
    for idx in range(0, len(items), size):
        yield items[idx : idx + size]


def minute_request_groups(periods: list[str], derive_higher_from_5m: bool) -> list[tuple[str, list[str]]]:
    if not derive_higher_from_5m:
        return [(period, [period]) for period in periods]
    groups: list[tuple[str, list[str]]] = []
    handled: set[str] = set()
    five_minute_targets = [period for period in periods if period in {"5m", "15m", "30m", "60m"}]
    if five_minute_targets:
        groups.append(("5m", five_minute_targets))
        handled.update(five_minute_targets)
    groups.extend((period, [period]) for period in periods if period not in handled)
    return groups


def ensure_stage_table(client, stage_table: str) -> None:
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
        """
    )


def _is_month_partitioned_minute_table(client, table: str) -> bool:
    """Destructive minute replacement is safe only when the target is date-partitioned."""
    rows = client.query(
        "SELECT partition_key FROM system.tables WHERE database = currentDatabase() AND name = %(table)s",
        parameters={"table": table},
    ).result_rows
    row = rows[0] if rows else None
    return bool(row and "toYYYYMM(datetime)" in str(row[0] or ""))


def _stable_id(period: str, code: str, dt: datetime) -> int:
    import hashlib

    key = f"{period}|{code}|{dt:%Y-%m-%d %H:%M:%S}".encode("utf-8")
    return int.from_bytes(hashlib.blake2b(key, digest_size=8).digest(), "big", signed=False)


def _as_clickhouse_market_datetime(value: Any) -> datetime:
    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()
    elif isinstance(value, str):
        parsed = pd.to_datetime(value, errors="coerce")
        if pd.isna(parsed):
            raise ValueError(f"invalid datetime value: {value!r}")
        value = pd.Timestamp(parsed).to_pydatetime()
    if not isinstance(value, datetime):
        raise TypeError(f"expected datetime value, got {type(value).__name__}")
    if value.tzinfo is not None:
        value = value.astimezone(BUSINESS_TZ)
    return value.replace(tzinfo=None)


def _field_frame(data: dict[str, Any], field: str) -> pd.DataFrame:
    value = data.get(field)
    if value is None:
        value = data.get(field.capitalize())
    return value.copy() if isinstance(value, pd.DataFrame) else pd.DataFrame()


def _normalize_datetime(value: Any) -> pd.Timestamp | None:
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        text = str(value)
        if text.endswith(".0"):
            text = text[:-2]
        if len(text) >= 14:
            parsed = pd.to_datetime(text[:14], format="%Y%m%d%H%M%S", errors="coerce")
        elif len(text) >= 12:
            parsed = pd.to_datetime(text[:12], format="%Y%m%d%H%M", errors="coerce")
        elif len(text) >= 8:
            parsed = pd.to_datetime(text[:8], format="%Y%m%d", errors="coerce")
    if pd.isna(parsed):
        return None
    return pd.Timestamp(parsed).tz_localize(None)


def rows_from_qmt(
    data: dict[str, Any],
    period: str,
    batch_codes: list[str],
    created_at: datetime,
    start_date: str = "",
    end_date: str = "",
) -> tuple[list[tuple], dict[str, int]]:
    close_df = _field_frame(data, "close")
    if close_df.empty:
        return [], {code: 0 for code in batch_codes}
    frames = {field: _field_frame(data, field) for field in FIELD_LIST}
    rows: list[tuple] = []
    counts: dict[str, int] = {}
    for code in batch_codes:
        if code in close_df.index:
            one = pd.DataFrame(index=close_df.columns)
            for field, frame in frames.items():
                one[field] = pd.to_numeric(frame.loc[code], errors="coerce") if code in frame.index else float("nan")
        elif code in close_df.columns:
            one = pd.DataFrame(index=close_df.index)
            for field, frame in frames.items():
                one[field] = pd.to_numeric(frame[code], errors="coerce") if code in frame.columns else float("nan")
        else:
            counts[code] = 0
            continue
        normalized_index = [_normalize_datetime(item) for item in one.index]
        one.index = normalized_index
        one = one[~pd.isna(one.index)].copy()
        one = one.dropna(subset=["open", "high", "low", "close"]).sort_index()
        one = one[~one.index.duplicated(keep="last")]
        if start_date.strip() and end_date.strip():
            start_ts = pd.Timestamp(start_date).normalize()
            end_ts = pd.Timestamp(end_date).normalize() + pd.Timedelta(days=1)
            one = one[(one.index >= start_ts) & (one.index < end_ts)]
        # QMT can return UTC-like or extended-session timestamps for a subset
        # of instruments. Persist only regular A-share close timestamps in the
        # canonical Asia/Shanghai wall-clock convention. This is enforced for
        # every period so bad timestamps cannot reach either stage or main.
        valid_times = REGULAR_ASHARE_5M_TIMES if period == "5m" else _regular_bar_times(period)
        if valid_times:
            one = one[[item.time() in valid_times for item in one.index]]
        if one.empty:
            counts[code] = 0
            continue
        count = 0
        for idx, values in one.iterrows():
            dt = idx.to_pydatetime()
            volume, amount = normalize_qmt_minute_values(values.get("volume"), values.get("amount"))
            rows.append(
                (
                    period,
                    code,
                    _as_clickhouse_market_datetime(dt),
                    float(values["open"] or 0),
                    float(values["high"] or 0),
                    float(values["low"] or 0),
                    float(values["close"] or 0),
                    volume,
                    amount,
                    _as_clickhouse_market_datetime(created_at),
                    _stable_id(period, code, dt),
                )
            )
            count += 1
        counts[code] = count
    return rows, counts


def _aggregate_5m_rows(rows: list[tuple], target_period: str) -> tuple[list[tuple], dict[str, int]]:
    group_size = {"15m": 3, "30m": 6, "60m": 12}.get(target_period)
    if not group_size:
        return rows, {}
    grouped: dict[tuple[str, str], list[tuple]] = {}
    for row in rows:
        source_dt = row[2]
        if getattr(source_dt, "tzinfo", None) is not None:
            source_dt = source_dt.replace(tzinfo=None)
        time_text = source_dt.strftime("%H:%M")
        if not (("09:35" <= time_text <= "11:30") or ("13:05" <= time_text <= "15:00")):
            continue
        day_key = source_dt.strftime("%Y-%m-%d")
        session_key = "am" if time_text <= "11:30" else "pm"
        grouped.setdefault((row[1], f"{day_key}|{session_key}"), []).append(row)

    out: list[tuple] = []
    counts: dict[str, int] = {}
    for (code, _session), items in sorted(grouped.items(), key=lambda item: (item[0][0], item[0][1])):
        items = sorted(items, key=lambda item: item[2])
        for idx in range(0, len(items), group_size):
            bucket = items[idx : idx + group_size]
            if len(bucket) < group_size:
                continue
            target_dt = bucket[-1][2]
            if getattr(target_dt, "tzinfo", None) is not None:
                target_dt = target_dt.replace(tzinfo=None)
            created_at = bucket[-1][9]
            out.append(
                (
                    target_period,
                    code,
                    _as_clickhouse_market_datetime(target_dt),
                    float(bucket[0][3]),
                    float(max(item[4] for item in bucket)),
                    float(min(item[5] for item in bucket)),
                    float(bucket[-1][6]),
                    float(sum(item[7] for item in bucket)),
                    float(sum(item[8] for item in bucket)),
                    _as_clickhouse_market_datetime(created_at),
                    _stable_id(target_period, code, target_dt),
                )
            )
            counts[code] = counts.get(code, 0) + 1
    return out, counts


def write_report(path: str, payload: dict[str, Any]) -> None:
    report = Path(path)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({**payload, "minute_unit_contract":MINUTE_UNIT_CONTRACT_ID}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def select_codes(client, args: argparse.Namespace) -> list[str]:
    codes = load_codes(client, args.codes, 0, False, args.end_date)
    if args.include_index and not args.codes.strip():
        index_filter = qmt_universe_filter_sql("index", code_expr="s.code", type_expr="s.type", name_expr="s.name")
        index_codes = [
            str(row[0]).upper()
            for row in client.query(
                f"SELECT DISTINCT s.code FROM stocks s WHERE (s.quit = 0 OR s.quit IS NULL) AND ({index_filter}) ORDER BY s.code"
            ).result_rows
            if row and row[0]
        ]
        codes.extend(index_codes)
        codes = list(dict.fromkeys(codes))
    if args.code_offset > 0:
        codes = codes[args.code_offset :]
    return codes[: args.limit] if args.limit > 0 else codes


def _fetch_batch_rows_worker(payload: dict[str, Any], queue: mp.Queue) -> None:
    try:
        market = QmtMiniMarketClient()
        market.connect()
        repaired_codes: list[str] = []
        if not payload["skip_download"]:
            market.download_history_data2(
                payload["batch_codes"],
                payload["request_period"],
                payload["start_compact"],
                payload["end_compact"],
            )
        data = market.get_market_data(
            field_list=FIELD_LIST,
            stock_list=payload["batch_codes"],
            period=payload["request_period"],
            start_time=payload["start_compact"],
            end_time=payload["end_compact"],
            count=-1,
            dividend_type=payload["dividend_type"],
            fill_data=False,
        )
        source_rows, counts = rows_from_qmt(
            data,
            payload["request_period"],
            payload["batch_codes"],
            payload["created_at"],
            payload["start_date"],
            payload["end_date"],
        )
        if (
            payload["repair_incomplete_cache"]
            and payload["skip_download"]
            and payload["request_period"] == "5m"
            and payload["start_date"] == payload["end_date"]
        ):
            min_rows = int(payload["min_source_5m_bars_per_day"] or 0)
            incomplete_codes = [code for code in payload["batch_codes"] if int(counts.get(code, 0) or 0) < min_rows]
            if incomplete_codes:
                market.download_history_data2(
                    incomplete_codes,
                    payload["request_period"],
                    payload["start_compact"],
                    payload["end_compact"],
                )
                repaired_codes = incomplete_codes
                data = market.get_market_data(
                    field_list=FIELD_LIST,
                    stock_list=payload["batch_codes"],
                    period=payload["request_period"],
                    start_time=payload["start_compact"],
                    end_time=payload["end_compact"],
                    count=-1,
                    dividend_type=payload["dividend_type"],
                    fill_data=False,
                )
                source_rows, counts = rows_from_qmt(
                    data,
                    payload["request_period"],
                    payload["batch_codes"],
                    payload["created_at"],
                    payload["start_date"],
                    payload["end_date"],
                )
        if payload["period"] in {"15m", "30m", "60m"} and payload["derive_higher_from_5m"]:
            rows, derived_counts = _aggregate_5m_rows(source_rows, payload["period"])
            counts = {code: derived_counts.get(code, 0) for code in payload["batch_codes"]}
        else:
            rows = source_rows
        queue.put({"ok": True, "rows": rows, "counts": counts, "repaired_codes": repaired_codes})
    except Exception as exc:
        queue.put({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


def _terminate_process_tree(proc: mp.Process) -> None:
    if not proc.is_alive():
        return
    if sys.platform.startswith("win") and proc.pid:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            return
        except Exception:
            pass
    proc.terminate()


def _fetch_batch_rows_with_timeout(
    *,
    period: str,
    request_period: str,
    batch_codes: list[str],
    start_compact: str,
    end_compact: str,
    dividend_type: str,
    created_at: datetime,
    derive_higher_from_5m: bool,
    skip_download: bool,
    repair_incomplete_cache: bool,
    min_source_5m_bars_per_day: int,
    timeout_sec: int,
) -> tuple[list[tuple], dict[str, int], list[str]]:
    ctx = mp.get_context("spawn")
    queue: mp.Queue = ctx.Queue()
    payload = {
        "period": period,
        "request_period": request_period,
        "batch_codes": batch_codes,
        "start_compact": start_compact,
        "end_compact": end_compact,
        "dividend_type": dividend_type,
        "created_at": created_at,
        "start_date": start_compact[:4] + "-" + start_compact[4:6] + "-" + start_compact[6:8],
        "end_date": end_compact[:4] + "-" + end_compact[4:6] + "-" + end_compact[6:8],
        "derive_higher_from_5m": derive_higher_from_5m,
        "skip_download": skip_download,
        "repair_incomplete_cache": repair_incomplete_cache,
        "min_source_5m_bars_per_day": min_source_5m_bars_per_day,
    }
    proc = ctx.Process(target=_fetch_batch_rows_worker, args=(payload, queue), daemon=True)
    result: dict[str, Any] | None = None
    try:
        proc.start()
        proc.join(max(1, int(timeout_sec)))
        if proc.is_alive():
            _terminate_process_tree(proc)
            proc.join(5)
            raise TimeoutError(f"qmt batch timeout period={period} request_period={request_period} codes={len(batch_codes)} timeout_sec={timeout_sec}")
        if queue.empty():
            raise RuntimeError(f"qmt batch worker exited without result exitcode={proc.exitcode}")
        result = queue.get()
    finally:
        if proc.is_alive():
            _terminate_process_tree(proc)
            proc.join(5)
        queue.cancel_join_thread()
        queue.close()
    if not result.get("ok"):
        raise RuntimeError(result.get("error") or "qmt batch worker failed")
    return result.get("rows") or [], result.get("counts") or {}, result.get("repaired_codes") or []


def _fetch_batch_rows_in_process(**kwargs: Any) -> tuple[list[tuple], dict[str, int], list[str]]:
    """Run one bulk QMT request in the current process.

    MiniQMT's batch downloader is already synchronous.  Spawning a fresh
    Python process around a large batch can stall its IPC handshake, while a
    direct batch call reports normal QMT progress and completes promptly.
    """
    payload = {
        "period": kwargs["period"],
        "request_period": kwargs["request_period"],
        "batch_codes": kwargs["batch_codes"],
        "start_compact": kwargs["start_compact"],
        "end_compact": kwargs["end_compact"],
        "dividend_type": kwargs["dividend_type"],
        "created_at": kwargs["created_at"],
        "start_date": kwargs["start_compact"][:4] + "-" + kwargs["start_compact"][4:6] + "-" + kwargs["start_compact"][6:8],
        "end_date": kwargs["end_compact"][:4] + "-" + kwargs["end_compact"][4:6] + "-" + kwargs["end_compact"][6:8],
        "derive_higher_from_5m": kwargs["derive_higher_from_5m"],
        "skip_download": kwargs["skip_download"],
        "repair_incomplete_cache": kwargs["repair_incomplete_cache"],
        "min_source_5m_bars_per_day": kwargs["min_source_5m_bars_per_day"],
    }

    class InlineQueue:
        value: dict[str, Any] | None = None

        def put(self, value: dict[str, Any]) -> None:
            self.value = value

    queue = InlineQueue()
    _fetch_batch_rows_worker(payload, queue)  # type: ignore[arg-type]
    result = queue.value or {"ok": False, "error": "qmt inline worker returned no result"}
    if not result.get("ok"):
        raise RuntimeError(result.get("error") or "qmt inline worker failed")
    return result.get("rows") or [], result.get("counts") or {}, result.get("repaired_codes") or []


def _is_nonrecoverable_qmt_error(error: str) -> bool:
    text = str(error or "")
    markers = (
        "QmtMiniCapabilityError",
        "ErrorID\" : 200005",
        "ErrorID\": 200005",
        "supplyHistoryData2",
        "does not expose required xtdata handler",
    )
    return any(marker in text for marker in markers)


def fetch_to_stage(args: argparse.Namespace) -> dict[str, Any]:
    client = ch_client()
    ensure_stage_table(client, args.stage_table)
    periods = [item.strip() for item in args.periods.split(",") if item.strip()]
    codes = select_codes(client, args)
    if args.reset_stage:
        # ALTER ... DELETE is asynchronous by default.  Waiting for the
        # mutation is essential: otherwise it can race the following batch
        # inserts and delete freshly-derived 15m/30m rows.
        client.command("SET mutations_sync = 2")
        period_sql = ",".join(quote_sql(period) for period in periods)
        code_filter = ""
        if args.reset_stage_codes_only:
            code_sql = ",".join(quote_sql(code) for code in codes) or "''"
            code_filter = f"AND code IN ({code_sql})"
        client.command(
            f"""
            ALTER TABLE {args.stage_table}
            DELETE WHERE period IN ({period_sql})
              {code_filter}
              AND toDate(datetime) >= toDate({quote_sql(args.start_date)})
              AND toDate(datetime) <= toDate({quote_sql(args.end_date)})
            SETTINGS mutations_sync = 1
            """
        )
    created_at = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    start_compact = args.start_date.replace("-", "")
    end_compact = args.end_date.replace("-", "")
    summary: dict[str, Any] = {"phase": "fetch", "codes": len(codes), "periods": {}, "failed_batches": 0}
    stats: dict[str, dict[str, Any]] = {
        period: {
            "request_period": period,
            "total_rows": 0,
            "empty_codes": [],
            "repair_cache_codes": [],
            "failed": [],
            "started": time.perf_counter(),
        }
        for period in periods
    }
    for request_period, target_periods in minute_request_groups(periods, args.derive_higher_from_5m):
        for target_period in target_periods:
            stats[target_period]["request_period"] = request_period
        for batch_no, batch_codes in enumerate(chunked(codes, args.batch_size), start=1):
            batch_started = time.perf_counter()
            attempts = 0
            source_rows: list[tuple] = []
            source_counts: dict[str, int] = {}
            repaired_cache_codes: list[str] = []
            error = ""
            try:
                for attempt in range(1, args.max_retries + 2):
                    attempts = attempt
                    try:
                        fetch_kwargs = dict(
                            period=request_period,
                            request_period=request_period,
                            batch_codes=batch_codes,
                            start_compact=start_compact,
                            end_compact=end_compact,
                            dividend_type=args.dividend_type,
                            created_at=created_at,
                            derive_higher_from_5m=False,
                            skip_download=args.skip_download,
                            repair_incomplete_cache=args.repair_incomplete_cache,
                            min_source_5m_bars_per_day=args.min_source_5m_bars_per_day,
                            timeout_sec=args.batch_timeout_sec,
                        )
                        if args.in_process:
                            fetch_kwargs.pop("timeout_sec")
                            source_rows, source_counts, repaired_cache_codes = _fetch_batch_rows_in_process(**fetch_kwargs)
                        else:
                            source_rows, source_counts, repaired_cache_codes = _fetch_batch_rows_with_timeout(**fetch_kwargs)
                        error = ""
                        break
                    except Exception as exc:
                        error = f"{type(exc).__name__}: {exc}"
                        if attempt > args.max_retries:
                            raise
                        time.sleep(args.retry_sleep)
                for target_period in target_periods:
                    if target_period == request_period:
                        rows = source_rows
                        counts = source_counts
                    elif target_period in {"15m", "30m", "60m"} and request_period == "5m":
                        rows, counts = _aggregate_5m_rows(source_rows, target_period)
                        counts = {code: counts.get(code, 0) for code in batch_codes}
                    else:
                        rows = []
                        counts = {code: 0 for code in batch_codes}
                    stats[target_period]["empty_codes"].extend([code for code, count in counts.items() if count == 0])
                    stats[target_period]["repair_cache_codes"].extend(repaired_cache_codes)
                    if rows:
                        client.insert(args.stage_table, rows, column_names=TARGET_COLUMNS)
                        stats[target_period]["total_rows"] += len(rows)
                    insert_request_log(
                        client,
                        task_name="qmt_xtquant_minute_backfill",
                        phase="fetch_stage",
                        period=target_period,
                        codes=batch_codes,
                        start_time=args.start_date,
                        end_time=args.end_date,
                        status="success",
                        attempts=attempts,
                        elapsed_sec=time.perf_counter() - batch_started,
                        rows_returned=len(rows),
                    )
                    log(
                        f"period={target_period} request_period={request_period} "
                        f"batch={batch_no} codes={len(batch_codes)} rows={len(rows)} attempts={attempts}"
                    )
            except Exception as exc:
                failed_item = {"batch": batch_no, "codes": batch_codes[:20], "error": f"{type(exc).__name__}: {exc}"}
                for target_period in target_periods:
                    stats[target_period]["failed"].append(failed_item)
                    insert_request_log(
                        client,
                        task_name="qmt_xtquant_minute_backfill",
                        phase="fetch_stage",
                        period=target_period,
                        codes=batch_codes,
                        start_time=args.start_date,
                        end_time=args.end_date,
                        status="failed",
                        attempts=attempts or 1,
                        elapsed_sec=time.perf_counter() - batch_started,
                        rows_returned=len(source_rows),
                        error=error or f"{type(exc).__name__}: {exc}",
                    )
                if _is_nonrecoverable_qmt_error(failed_item["error"]):
                    for target_period in target_periods:
                        stats[target_period]["nonrecoverable_error"] = failed_item["error"]
                    log(
                        "nonrecoverable qmt minute fetch error; "
                        f"stop remaining batches request_period={request_period} batch={batch_no}"
                    )
                    break
    for period in periods:
        period_stats = stats[period]
        failed = period_stats["failed"]
        summary["periods"][period] = {
            "request_period": period_stats["request_period"],
            "rows_inserted_this_run": int(period_stats["total_rows"] or 0),
            "empty_codes": len(set(period_stats["empty_codes"])),
            # Keep the complete source-empty list, not merely a count, so the
            # historical rolling repair can make one later QMT retry without
            # turning these non-obligation codes into final-audit gaps.
            "empty_code_list": sorted(set(period_stats["empty_codes"])),
            "repair_cache_codes": len(set(period_stats["repair_cache_codes"])),
            "repair_cache_sample": list(dict.fromkeys(period_stats["repair_cache_codes"]))[:20],
            "failed_batches": len(failed),
            "failed_sample": failed[:10],
            "nonrecoverable_error": period_stats.get("nonrecoverable_error"),
            "elapsed_sec": round(time.perf_counter() - period_stats["started"], 3),
        }
        summary["failed_batches"] += len(failed)
    write_report(args.report, {"summary": summary})
    if int(summary["failed_batches"] or 0) > 0:
        raise RuntimeError(f"qmt minute fetch has failed batches: {summary['failed_batches']} report={args.report}")
    return summary


def validate_stage(args: argparse.Namespace) -> dict[str, Any]:
    client = ch_client()
    codes = select_codes(client, args)
    code_sql = ",".join(quote_sql(code) for code in codes) or "''"
    periods = [item.strip() for item in args.periods.split(",") if item.strip()]
    result: dict[str, Any] = {"phase": "validate_stage", "codes": len(codes), "periods": {}}
    for period in periods:
        main_table = PERIOD_TO_MAIN_TABLE.get(period)
        stage_row = client.query(
            f"""
            SELECT count(), uniqExact(code), min(datetime), max(datetime)
            FROM {args.stage_table}
            WHERE period = {quote_sql(period)}
              AND code IN ({code_sql})
              AND toDate(datetime) >= toDate({quote_sql(args.start_date)})
              AND toDate(datetime) <= toDate({quote_sql(args.end_date)})
            """
        ).first_row
        if not main_table:
            result["periods"][period] = {"ok": False, "reason": "unsupported_period", "stage_rows": int(stage_row[0] or 0)}
            continue
        overlap = client.query(
            f"""
            SELECT
                count() AS overlap_rows,
                countIf(abs(q.close - k.close) > {args.price_tolerance}) AS close_diff_rows,
                max(abs(q.close - k.close)) AS max_close_abs_diff,
                avg(abs(q.close - k.close)) AS avg_close_abs_diff,
                countIf(abs(q.volume - k.volume) > {args.volume_tolerance}) AS volume_diff_rows
            FROM {args.stage_table} q
            INNER JOIN
            (
                SELECT code, datetime, close, volume
                FROM {main_table} FINAL
                WHERE code IN ({code_sql})
                  AND toDate(datetime) >= toDate({quote_sql(args.start_date)})
                  AND toDate(datetime) <= toDate({quote_sql(args.end_date)})
            ) k
                ON q.code = assumeNotNull(k.code) AND q.datetime = assumeNotNull(k.datetime)
            WHERE q.period = {quote_sql(period)}
              AND q.code IN ({code_sql})
              AND toDate(q.datetime) >= toDate({quote_sql(args.start_date)})
              AND toDate(q.datetime) <= toDate({quote_sql(args.end_date)})
            """
        ).first_row
        qmt_only = client.query(
            f"""
            SELECT count()
            FROM {args.stage_table} q
            LEFT JOIN
            (
                SELECT code, datetime
                FROM {main_table} FINAL
                WHERE code IN ({code_sql})
                  AND toDate(datetime) >= toDate({quote_sql(args.start_date)})
                  AND toDate(datetime) <= toDate({quote_sql(args.end_date)})
            ) k
                ON q.code = assumeNotNull(k.code) AND q.datetime = assumeNotNull(k.datetime)
            WHERE q.period = {quote_sql(period)}
              AND q.code IN ({code_sql})
              AND toDate(q.datetime) >= toDate({quote_sql(args.start_date)})
              AND toDate(q.datetime) <= toDate({quote_sql(args.end_date)})
              AND (k.code IS NULL OR k.code = '')
            """
        ).first_row[0]
        result["periods"][period] = {
            "ok": True,
            "main_table": main_table,
            "stage_rows": int(stage_row[0] or 0),
            "stage_codes": int(stage_row[1] or 0),
            "min_datetime": str(stage_row[2]) if stage_row[2] else None,
            "max_datetime": str(stage_row[3]) if stage_row[3] else None,
            "overlap_rows": int(overlap[0] or 0),
            "close_diff_rows": int(overlap[1] or 0),
            "max_close_abs_diff": float(overlap[2] or 0),
            "avg_close_abs_diff": float(overlap[3] or 0),
            "volume_diff_rows": int(overlap[4] or 0),
            "qmt_only_rows": int(qmt_only or 0),
        }
    write_report(args.report, {"summary": result})
    log("stage validation " + json.dumps(result, ensure_ascii=False))
    return result


def _eligible_codes_for_apply(client, args: argparse.Namespace, codes: list[str]) -> tuple[list[str], list[str]]:
    if not args.require_complete_5m_for_apply or args.start_date != args.end_date or not codes:
        return codes, []
    code_sql = ",".join(quote_sql(code) for code in codes) or "''"
    rows = client.query(
        f"""
        SELECT code, count() AS rows, min(datetime) AS min_dt, max(datetime) AS max_dt
        FROM {args.stage_table}
        WHERE period = '5m'
          AND code IN ({code_sql})
          AND toDate(datetime) = toDate({quote_sql(args.start_date)})
        GROUP BY code
        """
    ).result_rows
    eligible = {
        str(code)
        for code, row_count, min_dt, max_dt in rows
        if int(row_count or 0) >= args.min_5m_bars_per_day
        and str(min_dt or "") >= f"{args.start_date} 09:00:00"
        and str(max_dt or "") >= f"{args.start_date} 15:00:00"
    }
    skipped = [code for code in codes if code not in eligible]
    return [code for code in codes if code in eligible], skipped


def _final_main_visibility(
    client,
    *,
    stage_table: str,
    main_table: str,
    period: str,
    codes: list[str],
    start_date: str,
    end_date: str,
) -> dict[str, Any]:
    code_sql = ",".join(quote_sql(code) for code in codes) or "''"
    stage_filter = f"""
        period = {quote_sql(period)}
        AND code IN ({code_sql})
        AND toDate(datetime) >= toDate({quote_sql(start_date)})
        AND toDate(datetime) <= toDate({quote_sql(end_date)})
    """
    main_filter = f"""
        code IN ({code_sql})
        AND toDate(datetime) >= toDate({quote_sql(start_date)})
        AND toDate(datetime) <= toDate({quote_sql(end_date)})
    """
    stage = client.query(
        f"""
        SELECT
            count(),
            uniqExact(tuple(code, datetime)),
            uniqExact(code),
            min(datetime),
            max(datetime)
        FROM {stage_table} FINAL
        WHERE {stage_filter}
        """
    ).first_row
    main = client.query(
        f"""
        SELECT
            count(),
            uniqExact(tuple(code, datetime)),
            uniqExact(code),
            min(datetime),
            max(datetime)
        FROM {main_table} FINAL
        WHERE {main_filter}
        """
    ).first_row
    missing = client.query(
        f"""
        SELECT count()
        FROM
        (
            SELECT code, datetime
            FROM {stage_table} FINAL
            WHERE {stage_filter}
        ) AS s
        LEFT JOIN
        (
            SELECT code, datetime
            FROM {main_table} FINAL
            WHERE {main_filter}
        ) AS m
            ON s.code = m.code AND s.datetime = m.datetime
        WHERE m.code IS NULL OR m.code = ''
        """
    ).first_row[0]
    stage_rows = int(stage[0] or 0)
    stage_keys = int(stage[1] or 0)
    main_rows = int(main[0] or 0)
    main_keys = int(main[1] or 0)
    missing_keys = int(missing or 0)
    return {
        "stage_rows": stage_rows,
        "stage_keys": stage_keys,
        "stage_codes": int(stage[2] or 0),
        "stage_min_datetime": str(stage[3]) if stage[3] else None,
        "stage_max_datetime": str(stage[4]) if stage[4] else None,
        "main_final_rows": main_rows,
        "main_final_keys": main_keys,
        "main_final_codes": int(main[2] or 0),
        "main_final_min_datetime": str(main[3]) if main[3] else None,
        "main_final_max_datetime": str(main[4]) if main[4] else None,
        "stage_keys_missing_in_main_final": missing_keys,
        "ok": bool(missing_keys == 0 and (stage_keys == 0 or main_keys >= stage_keys)),
    }


def apply_stage(args: argparse.Namespace) -> dict[str, Any]:
    client = ch_client()
    codes = select_codes(client, args)
    apply_codes, skipped_codes = _eligible_codes_for_apply(client, args, codes)
    if skipped_codes:
        log(f"skip incomplete 5m codes before apply: count={len(skipped_codes)} sample={skipped_codes[:10]}")
    code_sql = ",".join(quote_sql(code) for code in apply_codes) or "''"
    periods = [item.strip() for item in args.periods.split(",") if item.strip()]
    result: dict[str, Any] = {
        "phase": "apply",
        "codes": len(codes),
        "apply_codes": len(apply_codes),
        "skipped_incomplete_5m": len(skipped_codes),
        "skipped_sample": skipped_codes[:20],
        "periods": {},
    }
    for period in periods:
        main_table = PERIOD_TO_MAIN_TABLE.get(period)
        if not main_table:
            result["periods"][period] = {"ok": False, "reason": "unsupported_period"}
            continue
        if not apply_codes:
            result["periods"][period] = {"ok": True, "main_table": main_table, "inserted_rows": 0, "skipped": "no_complete_5m_codes"}
            continue
        partitioned_target = _is_month_partitioned_minute_table(client, main_table)
        if partitioned_target:
            client.command(
                f"""
                ALTER TABLE {main_table}
                DELETE WHERE code IN ({code_sql})
                  AND toDate(datetime) >= toDate({quote_sql(args.start_date)})
                  AND toDate(datetime) <= toDate({quote_sql(args.end_date)})
                SETTINGS mutations_sync = 1
                """
            )
        client.command(
            f"""
            INSERT INTO {main_table} (code, datetime, open, high, low, close, volume, amount, created_at, id)
            SELECT s.code, s.datetime, s.open, s.high, s.low, s.close, s.volume, s.amount, s.created_at, s.id
            FROM
            (
                SELECT period, code, datetime, open, high, low, close, volume, amount, created_at, id
                FROM {args.stage_table} FINAL
                WHERE period = {quote_sql(period)}
                  AND code IN ({code_sql})
                  AND toDate(datetime) >= toDate({quote_sql(args.start_date)})
                  AND toDate(datetime) <= toDate({quote_sql(args.end_date)})
            ) AS s
            INNER JOIN trade_calendar AS c
                ON c.market = 'SH'
               AND c.is_trading = 1
               AND c.trade_date = toDate(s.datetime)
            WHERE s.period = {quote_sql(period)}
              AND s.code IN ({code_sql})
              AND toDate(s.datetime) >= toDate({quote_sql(args.start_date)})
              AND toDate(s.datetime) <= toDate({quote_sql(args.end_date)})
              AND {_regular_bar_time_sql(period, 's.datetime')}
            """
        )
        visibility = _final_main_visibility(
            client,
            stage_table=args.stage_table,
            main_table=main_table,
            period=period,
            codes=apply_codes,
            start_date=args.start_date,
            end_date=args.end_date,
        )
        result["periods"][period] = {
            "ok": bool(visibility["ok"]),
            "main_table": main_table,
            "inserted_rows": int(visibility["main_final_rows"]),
            "write_mode": "replace_partitioned" if partitioned_target else "append_only_unpartitioned",
            "verification": visibility,
        }
    write_report(args.report, {"summary": result})
    log("apply summary " + json.dumps(result, ensure_ascii=False))
    failed = [
        period
        for period, detail in result["periods"].items()
        if not bool(detail.get("ok"))
    ]
    if failed:
        raise RuntimeError(f"minute main-table FINAL visibility verification failed: {failed} report={args.report}")
    return result


def ensure_latest_5m_table(client, table_name: str) -> None:
    client.command(
        f"""
        CREATE TABLE IF NOT EXISTS {table_name}
        (
            trade_date Date,
            code String,
            datetime DateTime,
            open Float64,
            high Float64,
            low Float64,
            close Float64,
            volume Float64,
            amount Float64,
            source String,
            updated_at DateTime,
            id UInt64
        )
        ENGINE = ReplacingMergeTree(updated_at)
        ORDER BY (trade_date, code)
        """
    )


def apply_latest_5m(args: argparse.Namespace) -> dict[str, Any]:
    client = ch_client()
    ensure_latest_5m_table(client, args.latest_5m_table)
    codes = select_codes(client, args)
    code_sql = ",".join(quote_sql(code) for code in codes) or "''"
    now = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    now_sql = quote_sql(now.strftime("%Y-%m-%d %H:%M:%S"))
    client.command(
        f"""
        INSERT INTO {args.latest_5m_table}
            (trade_date, code, datetime, open, high, low, close, volume, amount, source, updated_at, id)
        SELECT
            toDate(source_datetime) AS trade_date,
            code,
            argMax(source_datetime, source_datetime) AS datetime,
            argMax(open, source_datetime) AS open,
            argMax(high, source_datetime) AS high,
            argMax(low, source_datetime) AS low,
            argMax(close, source_datetime) AS close,
            argMax(volume, source_datetime) AS volume,
            argMax(amount, source_datetime) AS amount,
            'qmt_xtquant_intraday_latest_5m' AS source,
            toDateTime({now_sql}) AS updated_at,
            cityHash64(code, toString(toDate(source_datetime))) AS id
        FROM
        (
            SELECT
                code,
                datetime AS source_datetime,
                open,
                high,
                low,
                close,
                volume,
                amount
            FROM {args.stage_table}
            WHERE period = '5m'
              AND code IN ({code_sql})
              AND toDate(datetime) >= toDate({quote_sql(args.start_date)})
              AND toDate(datetime) <= toDate({quote_sql(args.end_date)})
        )
        GROUP BY trade_date, code
        """
    )
    inserted = client.query(
        f"""
        SELECT count()
        FROM {args.stage_table}
        WHERE period = '5m'
          AND code IN ({code_sql})
          AND toDate(datetime) >= toDate({quote_sql(args.start_date)})
          AND toDate(datetime) <= toDate({quote_sql(args.end_date)})
        GROUP BY code
        """
    ).result_rows
    result = {
        "phase": "apply_latest_5m",
        "codes": len(codes),
        "latest_rows": len(inserted),
        "latest_table": args.latest_5m_table,
    }
    write_report(args.report, {"summary": result})
    log("apply latest 5m summary " + json.dumps(result, ensure_ascii=False))
    return result


def parse_args() -> argparse.Namespace:
    today = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    default_report = report_path("qmt_xtquant_minute_backfill_validate", f"minute_{today.replace('-', '')}.json")
    parser = argparse.ArgumentParser(description="Fetch and validate QMT/xtquant minute bars against ClickHouse minute tables.")
    parser.add_argument(
        "--phase",
        choices=["fetch", "validate-stage", "fetch-validate", "apply", "apply-latest-5m", "all"],
        default="validate-stage",
    )
    parser.add_argument("--start-date", default=today)
    parser.add_argument("--end-date", default=today)
    parser.add_argument("--codes", default="")
    parser.add_argument("--code-offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--include-index", action="store_true")
    parser.add_argument(
        "--index-codes",
        default=DEFAULT_INDEX_CODES,
        help="Comma-separated core index codes to include when --include-index is set.",
    )
    parser.add_argument("--periods", default="5m,15m,30m,60m")
    parser.add_argument("--batch-size", type=int, default=30)
    parser.add_argument("--stage-table", default=DEFAULT_STAGE_TABLE)
    parser.add_argument("--latest-5m-table", default=DEFAULT_LATEST_5M_TABLE)
    parser.add_argument("--reset-stage", action="store_true")
    parser.add_argument("--reset-stage-codes-only", action="store_true")
    parser.add_argument("--dividend-type", default="none", choices=["none", "front", "back"])
    parser.add_argument("--derive-higher-from-5m", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--derive-60m-from-30m", action=argparse.BooleanOptionalAction, default=True, help=argparse.SUPPRESS)
    parser.add_argument("--skip-download", action="store_true", help="Read QMT local cached history without calling download_history_data2 first.")
    parser.add_argument("--repair-incomplete-cache", action="store_true", help="When reading local QMT 5m cache, download and reread codes with incomplete single-day bars.")
    parser.add_argument("--batch-timeout-sec", type=int, default=180)
    parser.add_argument("--in-process", action="store_true", help="Run synchronous QMT bulk batches in this process; use for after-close full refresh.")
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--retry-sleep", type=float, default=1.0)
    parser.add_argument("--price-tolerance", type=float, default=0.001)
    parser.add_argument("--volume-tolerance", type=float, default=1.0)
    parser.add_argument("--require-complete-5m-for-apply", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--min-5m-bars-per-day", type=int, default=48)
    parser.add_argument("--min-source-5m-bars-per-day", type=int, default=48)
    parser.add_argument("--report", default=str(default_report))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    summaries: list[dict[str, Any]] = []
    if args.phase in {"fetch", "fetch-validate", "all"}:
        summaries.append(fetch_to_stage(args))
    if args.phase in {"validate-stage", "fetch-validate", "all"}:
        summaries.append(validate_stage(args))
    if args.phase in {"apply", "all"}:
        summaries.append(apply_stage(args))
    if args.phase == "apply-latest-5m":
        summaries.append(apply_latest_5m(args))
    write_report(args.report, {"ok": True, "phase": args.phase, "summaries": summaries})
    log("done " + json.dumps(summaries, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

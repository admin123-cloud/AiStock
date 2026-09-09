from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from scripts.qmtmini_daily_backfill_validate import ch_client, quote_sql
from utils.kline_store import filter_trading_day_tuples
from utils.paths import report_path


BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_PERIODS = "5m,15m,30m,60m"
DEFAULT_MIN_5M_BARS = 48
MINUTE_TABLES = {
    "5m": "kline_minute_5",
    "15m": "kline_minute_15",
    "30m": "kline_minute_30",
    "60m": "kline_minute_60",
}


def is_month_partitioned_minute_table(client, table: str) -> bool:
    rows = client.query(
        "SELECT partition_key FROM system.tables WHERE database = currentDatabase() AND name = %(table)s",
        parameters={"table": table},
    ).result_rows
    return bool(rows and "toYYYYMM(datetime)" in str(rows[0][0] or ""))


def log(message: str) -> None:
    print(f"[{datetime.now(BUSINESS_TZ).strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def json_default(value: Any) -> str:
    return str(value)


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def years_before(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year - years)
    except ValueError:
        return day.replace(year=day.year - years, day=28)


def parse_periods(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def universe_filter_sql(code_expr: str, universe: str) -> str:
    kinds = {part.strip().lower() for part in universe.split(",") if part.strip()}
    if not kinds or "all" in kinds:
        return "1"
    parts: list[str] = []
    if "stock" in kinds:
        parts.append(
            f"(match({code_expr}, '^(000|001|002|003|300|301)[0-9]{{3}}\\\\.SZ$') "
            f"OR match({code_expr}, '^(600|601|603|605|688)[0-9]{{3}}\\\\.SH$') "
            f"OR match({code_expr}, '^[0-9]{{6}}\\\\.BJ$'))"
        )
    if "index" in kinds:
        parts.append(f"match({code_expr}, '^(000|399)[0-9]{{3}}\\\\.(SH|SZ)$')")
    if "etf" in kinds:
        parts.append(f"match({code_expr}, '^(159|510|511|512|513|515|516|517|518|588)[0-9]{{3}}\\\\.(SH|SZ)$')")
    if "other" in kinds:
        known = universe_filter_sql(code_expr, "stock,index,etf")
        parts.append(f"NOT ({known})")
    return " OR ".join(parts) if parts else "1"


def chunked(items: list[str], size: int) -> Iterable[list[str]]:
    for idx in range(0, len(items), size):
        yield items[idx : idx + size]


def load_trading_dates(client, start_date: str, end_date: str) -> list[str]:
    rows = client.query(
        f"""
        SELECT DISTINCT trade_date
        FROM trade_calendar
        WHERE trade_date >= toDate({quote_sql(start_date)})
          AND trade_date <= toDate({quote_sql(end_date)})
          AND market = 'SH'
          AND is_trading = 1
        ORDER BY trade_date
        """
    ).result_rows
    if rows:
        return [str(row[0]) for row in rows]
    rows = client.query(
        f"""
        SELECT DISTINCT trade_date
        FROM kline_daily
        WHERE trade_date >= toDate({quote_sql(start_date)})
          AND trade_date <= toDate({quote_sql(end_date)})
        ORDER BY trade_date
        """
    ).result_rows
    return [str(row[0]) for row in rows]


def resolve_default_end_date(client) -> str:
    today = datetime.now(BUSINESS_TZ).date().strftime("%Y-%m-%d")
    rows = client.query(
        f"""
        SELECT max(trade_date)
        FROM trade_calendar
        WHERE trade_date <= toDate({quote_sql(today)})
          AND market = 'SH'
          AND is_trading = 1
        """
    ).result_rows
    if rows and rows[0][0]:
        return str(rows[0][0])
    rows = client.query("SELECT max(trade_date) FROM kline_daily").result_rows
    if rows and rows[0][0]:
        return str(rows[0][0])
    return today


def expected_codes_for_date(client, trade_date: str, universe: str = "stock,index") -> list[str]:
    code_filter = universe_filter_sql("code", universe)
    rows = client.query(
        f"""
        SELECT DISTINCT code
        FROM kline_daily
        WHERE trade_date = toDate({quote_sql(trade_date)})
          AND ({code_filter})
        ORDER BY code
        """
    ).result_rows
    return [str(row[0]) for row in rows if row and row[0]]


def issue_codes_for_date(
    client,
    trade_date: str,
    min_bars: int,
    include_orphan_incomplete: bool = False,
    universe: str = "stock,index",
) -> dict[str, Any]:
    expected_filter = universe_filter_sql("code", universe)
    orphan_predicate = (
        f"""
            a.bars < {int(min_bars)}
            OR toString(a.min_dt) < concat({quote_sql(trade_date)}, ' 09:00:00')
            OR toString(a.max_dt) < concat({quote_sql(trade_date)}, ' 15:00:00')
        """
        if include_orphan_incomplete
        else f"toString(a.min_dt) < concat({quote_sql(trade_date)}, ' 09:00:00')"
    )
    rows = client.query(
        f"""
        WITH expected AS (
            SELECT DISTINCT code
            FROM kline_daily
            WHERE trade_date = toDate({quote_sql(trade_date)})
              AND ({expected_filter})
        ),
        actual AS (
            SELECT
                assumeNotNull(code) AS code,
                count() AS bars,
                min(assumeNotNull(datetime)) AS min_dt,
                max(assumeNotNull(datetime)) AS max_dt
            FROM kline_minute_5
            WHERE toDate(assumeNotNull(datetime)) = toDate({quote_sql(trade_date)})
            GROUP BY code
        )
        SELECT
            e.code,
            ifNull(a.bars, 0) AS bars,
            ifNull(toString(a.min_dt), '') AS min_dt,
            ifNull(toString(a.max_dt), '') AS max_dt,
            multiIf(
                a.code IS NULL, 'missing',
                a.bars < {int(min_bars)}, 'incomplete',
                toString(a.min_dt) < concat({quote_sql(trade_date)}, ' 09:00:00'), 'bad_early_time',
                toString(a.max_dt) < concat({quote_sql(trade_date)}, ' 15:00:00'), 'incomplete_tail',
                'ok'
            ) AS reason
        FROM expected e
        LEFT JOIN actual a ON e.code = a.code
        WHERE reason != 'ok'
        UNION ALL
        SELECT
            a.code,
            a.bars,
            toString(a.min_dt) AS min_dt,
            toString(a.max_dt) AS max_dt,
            multiIf(
                a.bars < {int(min_bars)}, 'orphan_incomplete',
                toString(a.min_dt) < concat({quote_sql(trade_date)}, ' 09:00:00'), 'orphan_bad_early_time',
                toString(a.max_dt) < concat({quote_sql(trade_date)}, ' 15:00:00'), 'orphan_incomplete_tail',
                'orphan_unexpected'
            ) AS reason
        FROM actual a
        WHERE a.code NOT IN (SELECT code FROM expected)
          AND ({orphan_predicate})
        ORDER BY code
        """
    ).result_rows
    issues = [
        {"code": str(code), "bars": int(bars or 0), "min_dt": str(min_dt or ""), "max_dt": str(max_dt or ""), "reason": str(reason)}
        for code, bars, min_dt, max_dt, reason in rows
    ]
    return {
        "date": trade_date,
        "issue_count": len(issues),
        "codes": [item["code"] for item in issues],
        "issues": issues,
        "reason_counts": {
            reason: sum(1 for item in issues if item["reason"] == reason)
            for reason in sorted({item["reason"] for item in issues})
        },
    }


def cleanup_orphan_bad_early_for_date(client, args: argparse.Namespace, trade_date: str, issue_info: dict[str, Any]) -> list[str]:
    if not args.cleanup_orphan_bad_early or args.dry_run:
        return []
    codes = [
        item["code"]
        for item in issue_info.get("issues", [])
        if item.get("reason") == "orphan_bad_early_time" and item.get("code")
    ]
    if not codes:
        return []
    code_sql = ",".join(quote_sql(code) for code in sorted(set(codes)))
    for period in parse_periods(args.periods):
        table = MINUTE_TABLES.get(period)
        if not table:
            continue
        if not is_month_partitioned_minute_table(client, table):
            log(f"skip destructive orphan cleanup for unpartitioned {table}")
            continue
        client.command(
            f"""
            ALTER TABLE {table}
            DELETE WHERE code IN ({code_sql})
              AND toDate(datetime) = toDate({quote_sql(trade_date)})
            SETTINGS mutations_sync = 1
            """
        )
    cleaned = sorted(set(codes))
    log(f"cleanup orphan_bad_early date={trade_date} codes={len(cleaned)}")
    return cleaned


def stable_minute_id(period: str, code: str, dt: datetime) -> int:
    key = f"{period}|{code}|{dt:%Y-%m-%d %H:%M:%S}".encode("utf-8")
    return int.from_bytes(hashlib.blake2b(key, digest_size=8).digest(), "big", signed=False)


def as_datetime(value: Any) -> datetime:
    if hasattr(value, "to_pydatetime"):
        return value.to_pydatetime()
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def shift_complete_bad_early_5m_for_date(client, args: argparse.Namespace, trade_date: str, issue_info: dict[str, Any]) -> list[str]:
    if not args.shift_complete_bad_early_5m or args.dry_run:
        return []
    codes = [
        item["code"]
        for item in issue_info.get("issues", [])
        if item.get("reason") == "bad_early_time" and int(item.get("bars") or 0) >= int(args.min_5m_bars)
    ]
    if not codes:
        return []
    if not is_month_partitioned_minute_table(client, "kline_minute_5"):
        log("skip destructive bad-early cleanup for unpartitioned kline_minute_5")
        return []
    code_sql = ",".join(quote_sql(code) for code in sorted(set(codes)))
    df = client.query_df(
        f"""
        SELECT code, datetime, open, high, low, close, volume, amount, created_at
        FROM kline_minute_5
        WHERE code IN ({code_sql})
          AND toDate(datetime) = toDate({quote_sql(trade_date)})
          AND datetime < toDateTime(concat({quote_sql(trade_date)}, ' 09:00:00'))
        ORDER BY code, datetime
        """
    )
    if df.empty:
        return []
    rows = []
    shifted_codes: set[str] = set()
    for row in df.itertuples(index=False):
        code = str(row.code)
        new_dt = as_datetime(row.datetime) + timedelta(hours=8)
        created_at = as_datetime(row.created_at) if row.created_at is not None else datetime.now(BUSINESS_TZ).replace(tzinfo=None)
        rows.append(
            [
                code,
                new_dt,
                row.open,
                row.high,
                row.low,
                row.close,
                row.volume,
                row.amount,
                created_at,
                stable_minute_id("5m", code, new_dt),
            ]
        )
        shifted_codes.add(code)
    rows = filter_trading_day_tuples(
        "5m",
        [tuple(row) for row in rows],
        ["code", "datetime", "open", "high", "low", "close", "volume", "amount", "created_at", "id"],
    )
    if not rows:
        log(f"shift bad_early_5m blocked by trade_calendar guard date={trade_date}")
        return []
    client.insert(
        "kline_minute_5",
        rows,
        column_names=["code", "datetime", "open", "high", "low", "close", "volume", "amount", "created_at", "id"],
    )
    client.command(
        f"""
        ALTER TABLE kline_minute_5
        DELETE WHERE code IN ({code_sql})
          AND toDate(datetime) = toDate({quote_sql(trade_date)})
          AND datetime < toDateTime(concat({quote_sql(trade_date)}, ' 09:00:00'))
        SETTINGS mutations_sync = 1
        """
    )
    shifted = sorted(shifted_codes)
    log(f"shift bad_early_5m date={trade_date} codes={len(shifted)} rows={len(rows)}")
    return shifted


def build_minute_cmd(
    args: argparse.Namespace,
    *,
    phase: str,
    trade_date: str,
    codes: list[str],
    report_file: Path,
    repair_mode: bool,
) -> list[str]:
    cmd = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "qmt_xtquant_minute_backfill_validate.py"),
        "--phase",
        phase,
        "--start-date",
        trade_date,
        "--end-date",
        trade_date,
        "--codes",
        ",".join(codes),
        "--periods",
        args.periods,
        "--batch-size",
        str(args.qmt_batch_size),
        "--batch-timeout-sec",
        str(args.batch_timeout_sec),
        "--max-retries",
        str(args.max_retries),
        "--retry-sleep",
        str(args.retry_sleep),
        "--min-5m-bars-per-day",
        str(args.min_5m_bars),
        "--min-source-5m-bars-per-day",
        str(args.min_5m_bars),
        "--reset-stage",
        "--report",
        str(report_file),
    ]
    if repair_mode:
        cmd.extend(["--skip-download", "--repair-incomplete-cache"])
        if args.repair_qmt_batch_size:
            cmd[cmd.index("--batch-size") + 1] = str(args.repair_qmt_batch_size)
    return cmd


def run_cmd(cmd: list[str], timeout_sec: int, dry_run: bool) -> dict[str, Any]:
    started = time.perf_counter()
    if dry_run:
        return {"ok": True, "dry_run": True, "elapsed_sec": 0.0, "cmd": cmd}
    proc = subprocess.Popen(
        cmd,
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        timed_out = True
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, text=True)
        stdout, stderr = proc.communicate(timeout=10)
    return {
        "ok": proc.returncode == 0 and not timed_out,
        "returncode": proc.returncode,
        "timed_out": timed_out,
        "elapsed_sec": round(time.perf_counter() - started, 3),
        "stdout_tail": (stdout or "")[-6000:],
        "stderr_tail": (stderr or "")[-6000:],
        "cmd": cmd,
    }


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, default=json_default) + "\n")


def load_done_keys(progress_file: Path) -> set[str]:
    if not progress_file.exists():
        return set()
    done: set[str] = set()
    with progress_file.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get("ok") and item.get("key"):
                done.add(str(item["key"]))
    return done


def run_backfill_for_date(
    client,
    args: argparse.Namespace,
    trade_date: str,
    run_dir: Path,
    progress_file: Path,
    done_keys: set[str],
) -> dict[str, Any]:
    issue_info = issue_codes_for_date(client, trade_date, args.min_5m_bars, args.include_orphan_incomplete, args.universe)
    backfill_reasons = {"missing", "incomplete", "incomplete_tail"}
    codes = [item["code"] for item in issue_info["issues"] if item["reason"] in backfill_reasons]
    if not codes:
        return {"date": trade_date, "ok": True, "skipped": "no_backfill_issues", "issues_before": issue_info}
    chunks = list(chunked(codes, args.code_chunk_size))
    failures: list[dict[str, Any]] = []
    applied = 0
    for idx, code_chunk in enumerate(chunks, start=1):
        key = f"backfill|{trade_date}|{idx}|{','.join(code_chunk[:3])}|{len(code_chunk)}"
        if args.resume and key in done_keys:
            log(f"skip done backfill date={trade_date} chunk={idx}/{len(chunks)} codes={len(code_chunk)}")
            continue
        report_file = run_dir / "chunk_reports" / f"backfill_{trade_date.replace('-', '')}_{idx:04d}.json"
        cmd = build_minute_cmd(args, phase="all", trade_date=trade_date, codes=code_chunk, report_file=report_file, repair_mode=False)
        log(f"backfill date={trade_date} chunk={idx}/{len(chunks)} codes={len(code_chunk)}")
        result = run_cmd(cmd, args.chunk_timeout_sec, args.dry_run)
        event = {
            "ts": datetime.now(BUSINESS_TZ).isoformat(),
            "key": key,
            "stage": "backfill",
            "date": trade_date,
            "chunk": idx,
            "chunks": len(chunks),
            "codes": len(code_chunk),
            "ok": result.get("ok"),
            "report": str(report_file),
            "result": result,
        }
        append_jsonl(progress_file, event)
        if result.get("ok"):
            done_keys.add(key)
            applied += len(code_chunk)
        else:
            failures.append(event)
            if args.stop_on_error:
                break
    after = issue_codes_for_date(client, trade_date, args.min_5m_bars, args.include_orphan_incomplete, args.universe) if not args.dry_run else {}
    return {
        "date": trade_date,
        "ok": not failures,
        "stage": "backfill",
        "issues_before": issue_info,
        "issues_after": after,
        "chunks": len(chunks),
        "codes_attempted": applied,
        "failures": failures[:5],
    }


def run_repair_for_date(
    client,
    args: argparse.Namespace,
    trade_date: str,
    run_dir: Path,
    progress_file: Path,
    done_keys: set[str],
) -> dict[str, Any]:
    issue_info = issue_codes_for_date(client, trade_date, args.min_5m_bars, args.include_orphan_incomplete, args.universe)
    shifted_bad_early = shift_complete_bad_early_5m_for_date(client, args, trade_date, issue_info)
    if shifted_bad_early:
        append_jsonl(
            progress_file,
            {
                "ts": datetime.now(BUSINESS_TZ).isoformat(),
                "key": f"shift_bad_early_5m|{trade_date}",
                "stage": "shift_bad_early_5m",
                "date": trade_date,
                "codes": shifted_bad_early,
                "ok": True,
            },
        )
        issue_info = issue_codes_for_date(client, trade_date, args.min_5m_bars, args.include_orphan_incomplete, args.universe)
    cleaned_orphans = cleanup_orphan_bad_early_for_date(client, args, trade_date, issue_info)
    if cleaned_orphans:
        append_jsonl(
            progress_file,
            {
                "ts": datetime.now(BUSINESS_TZ).isoformat(),
                "key": f"cleanup_orphan_bad_early|{trade_date}",
                "stage": "cleanup_orphan_bad_early",
                "date": trade_date,
                "codes": cleaned_orphans,
                "ok": True,
            },
        )
        issue_info = issue_codes_for_date(client, trade_date, args.min_5m_bars, args.include_orphan_incomplete, args.universe)
    codes = issue_info["codes"]
    if not codes:
        return {
            "date": trade_date,
            "ok": True,
            "skipped": "no_issues",
            "issues_before": issue_info,
            "shifted_bad_early_5m": shifted_bad_early,
            "cleaned_orphan_bad_early": cleaned_orphans,
        }
    chunks = list(chunked(codes, args.repair_code_chunk_size))
    failures: list[dict[str, Any]] = []
    applied = 0
    for idx, code_chunk in enumerate(chunks, start=1):
        key = f"repair|{trade_date}|{idx}|{','.join(code_chunk[:3])}|{len(code_chunk)}"
        if args.resume and key in done_keys:
            log(f"skip done repair date={trade_date} chunk={idx}/{len(chunks)} codes={len(code_chunk)}")
            continue
        report_file = run_dir / "chunk_reports" / f"repair_{trade_date.replace('-', '')}_{idx:04d}.json"
        cmd = build_minute_cmd(args, phase="all", trade_date=trade_date, codes=code_chunk, report_file=report_file, repair_mode=True)
        log(f"repair date={trade_date} chunk={idx}/{len(chunks)} codes={len(code_chunk)} reasons={issue_info['reason_counts']}")
        result = run_cmd(cmd, args.chunk_timeout_sec, args.dry_run)
        event = {
            "ts": datetime.now(BUSINESS_TZ).isoformat(),
            "key": key,
            "stage": "repair",
            "date": trade_date,
            "chunk": idx,
            "chunks": len(chunks),
            "codes": len(code_chunk),
            "ok": result.get("ok"),
            "report": str(report_file),
            "result": result,
        }
        append_jsonl(progress_file, event)
        if result.get("ok"):
            done_keys.add(key)
            applied += len(code_chunk)
        else:
            failures.append(event)
            if args.stop_on_error:
                break
    after = issue_codes_for_date(client, trade_date, args.min_5m_bars, args.include_orphan_incomplete, args.universe) if not args.dry_run else {}
    return {
        "date": trade_date,
        "ok": not failures,
        "stage": "repair",
        "issues_before": issue_info,
        "issues_after": after,
        "shifted_bad_early_5m": shifted_bad_early,
        "cleaned_orphan_bad_early": cleaned_orphans,
        "chunks": len(chunks),
        "codes_attempted": applied,
        "failures": failures[:5],
    }


def run_workflow(args: argparse.Namespace) -> dict[str, Any]:
    client = ch_client()
    end_date = args.end_date or resolve_default_end_date(client)
    start_date = args.start_date or years_before(parse_date(end_date), args.years).strftime("%Y-%m-%d")
    trading_dates = load_trading_dates(client, start_date, end_date)
    if args.order == "desc":
        trading_dates = list(reversed(trading_dates))
    if args.max_dates > 0:
        trading_dates = trading_dates[: args.max_dates]
    stamp = datetime.now(BUSINESS_TZ).strftime("%Y%m%d_%H%M%S")
    run_dir = report_path("qmt_xtquant_three_year_minute_backfill", f"run_{stamp}")
    run_dir.mkdir(parents=True, exist_ok=True)
    progress_file = Path(args.progress_file) if args.progress_file else run_dir / "progress.jsonl"
    done_keys = load_done_keys(progress_file) if args.resume else set()

    log(
        "workflow "
        + json.dumps(
            {
                "mode": args.mode,
                "start_date": start_date,
                "end_date": end_date,
                "trading_dates": len(trading_dates),
                "run_dir": str(run_dir),
                "progress_file": str(progress_file),
                "dry_run": args.dry_run,
            },
            ensure_ascii=False,
        )
    )

    date_results: list[dict[str, Any]] = []
    failures = 0
    for idx, trade_date in enumerate(trading_dates, start=1):
        log(f"date {idx}/{len(trading_dates)} {trade_date}")
        if args.mode in {"full", "backfill"}:
            result = run_backfill_for_date(client, args, trade_date, run_dir, progress_file, done_keys)
            date_results.append(result)
            failures += 0 if result.get("ok") else 1
            append_jsonl(progress_file, {"ts": datetime.now(BUSINESS_TZ).isoformat(), "stage": "date_backfill_summary", **result})
            if args.stop_on_error and not result.get("ok"):
                break
        if args.mode in {"full", "repair"}:
            result = run_repair_for_date(client, args, trade_date, run_dir, progress_file, done_keys)
            date_results.append(result)
            failures += 0 if result.get("ok") else 1
            append_jsonl(progress_file, {"ts": datetime.now(BUSINESS_TZ).isoformat(), "stage": "date_repair_summary", **result})
            if args.stop_on_error and not result.get("ok"):
                break
        if args.mode == "verify":
            issue_info = issue_codes_for_date(client, trade_date, args.min_5m_bars, args.include_orphan_incomplete, args.universe)
            result = {"date": trade_date, "ok": issue_info["issue_count"] == 0, "stage": "verify", "issues": issue_info}
            date_results.append(result)
            append_jsonl(progress_file, {"ts": datetime.now(BUSINESS_TZ).isoformat(), "stage": "date_verify_summary", **result})

    final_issues = []
    final_verification = "skipped_dry_run" if args.dry_run else "completed"
    if not args.dry_run:
        for trade_date in trading_dates:
            issue_info = issue_codes_for_date(client, trade_date, args.min_5m_bars, args.include_orphan_incomplete, args.universe)
            if issue_info["issue_count"]:
                final_issues.append(
                    {
                        "date": trade_date,
                        "issue_count": issue_info["issue_count"],
                        "reason_counts": issue_info["reason_counts"],
                        "sample": issue_info["issues"][:20],
                    }
                )
    ok = failures == 0 and (args.mode != "verify" or not final_issues)
    validation_status = "passed" if ok else ("failed" if failures else "issues_remaining")
    summary = {
        "ok": ok,
        "validation_status": validation_status,
        "mode": args.mode,
        "start_date": start_date,
        "end_date": end_date,
        "trading_dates": len(trading_dates),
        "failures": failures,
        "final_verification": final_verification,
        "final_issue_dates": len(final_issues),
        "final_issue_sample": final_issues[:30],
        "progress_file": str(progress_file),
        "run_dir": str(run_dir),
        "date_results_sample": date_results[-20:],
    }
    summary_file = run_dir / "summary.json"
    summary_file.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=json_default), encoding="utf-8")
    summary["summary_file"] = str(summary_file)
    log("summary " + json.dumps({k: summary[k] for k in ["ok", "failures", "final_issue_dates", "summary_file"]}, ensure_ascii=False))
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill the latest years of QMT minute bars and then repair each trading day with completeness guards."
    )
    parser.add_argument("--mode", choices=["full", "backfill", "repair", "verify"], default="full")
    parser.add_argument("--years", type=int, default=3)
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--order", choices=["asc", "desc"], default="asc")
    parser.add_argument("--periods", default=DEFAULT_PERIODS)
    parser.add_argument("--min-5m-bars", type=int, default=DEFAULT_MIN_5M_BARS)
    parser.add_argument("--universe", default="stock,index", help="Comma-separated: stock,index,etf,other,all")
    parser.add_argument("--include-orphan-incomplete", action="store_true")
    parser.add_argument("--cleanup-orphan-bad-early", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--shift-complete-bad-early-5m", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--code-chunk-size", type=int, default=80)
    parser.add_argument("--repair-code-chunk-size", type=int, default=40)
    parser.add_argument("--qmt-batch-size", type=int, default=30)
    parser.add_argument("--repair-qmt-batch-size", type=int, default=1)
    parser.add_argument("--batch-timeout-sec", type=int, default=180)
    parser.add_argument("--chunk-timeout-sec", type=int, default=7200)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--retry-sleep", type=float, default=1.0)
    parser.add_argument("--max-dates", type=int, default=0)
    parser.add_argument("--progress-file", default="")
    parser.add_argument("--resume", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--stop-on-error", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--print-full-summary", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def compact_summary(summary: dict[str, Any]) -> dict[str, Any]:
    failures = summary.get("failures") or []
    failures_count = failures if isinstance(failures, int) else len(failures)
    issue_sample = [
        {
            "date": item.get("date"),
            "issue_count": item.get("issue_count"),
            "reason_counts": item.get("reason_counts"),
        }
        for item in summary.get("final_issue_sample", [])[:10]
    ]
    return {
        "ok": summary.get("ok"),
        "validation_status": summary.get("validation_status"),
        "mode": summary.get("mode"),
        "start_date": summary.get("start_date"),
        "end_date": summary.get("end_date"),
        "trading_dates": summary.get("trading_dates"),
        "failures_count": failures_count,
        "final_issue_dates": summary.get("final_issue_dates"),
        "final_issue_sample": issue_sample,
        "progress_file": summary.get("progress_file"),
        "summary_file": summary.get("summary_file"),
    }


def main() -> int:
    args = parse_args()
    summary = run_workflow(args)
    payload = summary if args.print_full_summary else compact_summary(summary)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=json_default))
    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

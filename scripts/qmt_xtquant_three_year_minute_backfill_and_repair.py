from __future__ import annotations

import argparse
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
from utils.paths import report_path


BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_PERIODS = "5m,15m,30m,60m"
DEFAULT_MIN_5M_BARS = 48


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


def expected_codes_for_date(client, trade_date: str) -> list[str]:
    rows = client.query(
        f"""
        SELECT DISTINCT code
        FROM kline_daily
        WHERE trade_date = toDate({quote_sql(trade_date)})
        ORDER BY code
        """
    ).result_rows
    return [str(row[0]) for row in rows if row and row[0]]


def issue_codes_for_date(client, trade_date: str, min_bars: int) -> dict[str, Any]:
    rows = client.query(
        f"""
        WITH expected AS (
            SELECT DISTINCT code
            FROM kline_daily
            WHERE trade_date = toDate({quote_sql(trade_date)})
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
        ORDER BY e.code
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
    proc = subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_sec,
    )
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "elapsed_sec": round(time.perf_counter() - started, 3),
        "stdout_tail": (proc.stdout or "")[-6000:],
        "stderr_tail": (proc.stderr or "")[-6000:],
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
    issue_info = issue_codes_for_date(client, trade_date, args.min_5m_bars)
    codes = issue_info["codes"]
    if not codes:
        return {"date": trade_date, "ok": True, "skipped": "already_complete", "issues_before": issue_info}
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
    after = issue_codes_for_date(client, trade_date, args.min_5m_bars) if not args.dry_run else {}
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
    issue_info = issue_codes_for_date(client, trade_date, args.min_5m_bars)
    codes = issue_info["codes"]
    if not codes:
        return {"date": trade_date, "ok": True, "skipped": "no_issues", "issues_before": issue_info}
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
    after = issue_codes_for_date(client, trade_date, args.min_5m_bars) if not args.dry_run else {}
    return {
        "date": trade_date,
        "ok": not failures,
        "stage": "repair",
        "issues_before": issue_info,
        "issues_after": after,
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
            issue_info = issue_codes_for_date(client, trade_date, args.min_5m_bars)
            result = {"date": trade_date, "ok": issue_info["issue_count"] == 0, "stage": "verify", "issues": issue_info}
            date_results.append(result)
            append_jsonl(progress_file, {"ts": datetime.now(BUSINESS_TZ).isoformat(), "stage": "date_verify_summary", **result})

    final_issues = []
    final_verification = "skipped_dry_run" if args.dry_run else "completed"
    if not args.dry_run:
        for trade_date in trading_dates:
            issue_info = issue_codes_for_date(client, trade_date, args.min_5m_bars)
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
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    summary = run_workflow(args)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=json_default))
    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

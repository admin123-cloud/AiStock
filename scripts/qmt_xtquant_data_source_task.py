from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
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

from scripts.qmtmini_daily_backfill_validate import ch_client, load_codes
from utils.paths import report_path, runtime_path


DEFAULT_MINUTE_PERIODS = ["5m", "15m", "30m", "60m"]


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def _json_default(value: Any) -> str:
    return str(value)


def _run_subprocess(cmd: list[str], timeout: int) -> dict[str, Any]:
    started = time.perf_counter()
    proc = subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "elapsed_sec": round(time.perf_counter() - started, 3),
        "stdout_tail": (proc.stdout or "")[-4000:],
        "stderr_tail": (proc.stderr or "")[-4000:],
        "cmd": cmd,
    }


def run_date_repair(args: argparse.Namespace) -> dict[str, Any]:
    client = ch_client()
    codes = load_codes(client, args.codes, args.limit, args.include_index, args.end_date)
    report_dir = report_path("qmt_xtquant_data_source_task")
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d_%H%M%S")
    daily_report = report_dir / f"daily_{args.start_date}_{args.end_date}_{stamp}.json"

    daily_cmd = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "qmtmini_daily_backfill_validate.py"),
        "--phase",
        args.daily_phase,
        "--start-date",
        args.start_date,
        "--end-date",
        args.end_date,
        "--batch-size",
        str(args.daily_batch_size),
        "--max-retries",
        str(args.max_retries),
        "--retry-sleep",
        str(args.retry_sleep),
        "--report",
        str(daily_report),
    ]
    if args.codes:
        daily_cmd.extend(["--codes", args.codes])
    if args.limit:
        daily_cmd.extend(["--limit", str(args.limit)])
    if args.include_index:
        daily_cmd.append("--include-index")
    if args.reset_stage:
        daily_cmd.append("--reset-stage")

    log(f"run QMT daily phase={args.daily_phase} date={args.start_date}~{args.end_date}")
    daily_result = _run_subprocess(daily_cmd, timeout=args.daily_timeout_sec)

    minute_result: dict[str, Any] = {"skipped": True, "reason": "minute disabled"}
    if args.with_minutes:
        minute_report_dir = report_dir / f"minute_gap_{args.start_date}_{args.end_date}_{stamp}"
        minute_cmd = [
            sys.executable,
            str(REPO_ROOT / "scripts" / "qmt_xtquant_minute_gap_audit_repair.py"),
            "--mode",
            "repair",
            "--start-date",
            args.start_date,
            "--end-date",
            args.end_date,
            "--periods",
            args.minute_periods,
            "--repair-batch-size",
            str(args.minute_batch_size),
            "--max-retries",
            str(args.max_retries),
            "--retry-sleep",
            str(args.retry_sleep),
            "--batch-timeout-sec",
            str(args.minute_batch_timeout_sec),
            "--repair-code-chunk-size",
            "1",
            "--report-dir",
            str(minute_report_dir),
        ]
        if args.codes:
            minute_cmd.extend(["--codes", args.codes])

        log(f"run QMT minute phase={args.minute_phase} periods={args.minute_periods} date={args.start_date}~{args.end_date}")
        minute_result = _run_subprocess(minute_cmd, timeout=args.minute_timeout_sec)
        minute_result["report_path"] = str(minute_report_dir / "summary.json")
        minute_result["report_dir"] = str(minute_report_dir)

    ok = bool(daily_result.get("ok")) and (bool(minute_result.get("ok")) if not minute_result.get("skipped") else True)
    summary = {
        "ok": ok,
        "source": "qmt_xtquant",
        "mode": "date_repair",
        "start_date": args.start_date,
        "end_date": args.end_date,
        "codes": len(codes),
        "daily": {
            **daily_result,
            "report_path": str(daily_report),
        },
        "minute": minute_result,
        "validation_status": "passed" if ok else "failed",
    }
    out_report = Path(args.report) if args.report else report_dir / f"summary_{args.start_date}_{args.end_date}_{stamp}.json"
    out_report.parent.mkdir(parents=True, exist_ok=True)
    out_report.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    summary["report_path"] = str(out_report)
    log("summary " + json.dumps({k: summary[k] for k in ["ok", "source", "mode", "codes", "validation_status", "report_path"]}, ensure_ascii=False))
    return summary


def run_minute_gap_repair(args: argparse.Namespace) -> dict[str, Any]:
    report_dir = Path(args.minute_report_dir) if args.minute_report_dir else report_path(
        "qmt_xtquant_data_source_task",
        f"minute_gap_{args.scenario}_{args.start_date}_{args.end_date}_{datetime.now(ZoneInfo('Asia/Shanghai')):%Y%m%d_%H%M%S}",
    )
    report_dir.mkdir(parents=True, exist_ok=True)
    source_empty_retries = (
        _retry_after_close_source_empty_codes(
            args,
            report_dir,
            _load_trade_dates(args.start_date, args.end_date),
        )
        if args.retry_after_close_source_empty
        else []
    )
    cmd = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "qmt_xtquant_minute_gap_audit_repair.py"),
        "--mode",
        "repair",
        "--start-date",
        args.start_date,
        "--end-date",
        args.end_date,
        "--periods",
        args.minute_periods,
        "--universe",
        args.universe,
        "--audit-code-chunk-size",
        str(args.audit_code_chunk_size),
        "--repair-code-chunk-size",
        str(args.repair_code_chunk_size),
        "--repair-workers",
        str(args.repair_workers),
        "--repair-batch-size",
        str(args.minute_batch_size),
        "--batch-timeout-sec",
        str(args.minute_batch_timeout_sec),
        "--chunk-timeout-sec",
        str(args.minute_chunk_timeout_sec),
        "--max-retries",
        str(args.max_retries),
        "--retry-sleep",
        str(args.retry_sleep),
        "--report-dir",
        str(report_dir),
        # The intraday full-push collector has already populated QMT's local
        # cache for most codes.  Read that cache first; the minute worker only
        # calls download_history_data2 again for a code whose 5m source is
        # still incomplete.  This keeps the after-close run bounded and lets
        # all derived periods share one repaired 5m source.
        "--skip-download",
        "--repair-incomplete-cache",
    ]
    if args.codes:
        cmd.extend(["--codes", args.codes])
    if args.issue_file:
        cmd.extend(["--issue-file", args.issue_file])
    if args.repair_code_offset > 0:
        cmd.extend(["--repair-code-offset", str(args.repair_code_offset)])
    if args.max_repair_codes > 0:
        cmd.extend(["--max-repair-codes", str(args.max_repair_codes)])
    if args.stop_on_error:
        cmd.append("--stop-on-error")

    log(
        "run QMT unified minute collector "
        + json.dumps(
            {
                "scenario": args.scenario,
                "date": f"{args.start_date}~{args.end_date}",
                "periods": args.minute_periods,
                "codes": len([item for item in str(args.codes or "").split(",") if item.strip()]),
                "report_dir": str(report_dir),
            },
            ensure_ascii=False,
        )
    )
    minute_result = _run_subprocess(cmd, timeout=args.minute_timeout_sec)
    summary_file = report_dir / "summary.json"
    worker_summary: dict[str, Any] = {}
    if summary_file.exists():
        try:
            worker_summary = json.loads(summary_file.read_text(encoding="utf-8"))
        except Exception as exc:
            worker_summary = {"parse_error": str(exc)}
    ok = bool(minute_result.get("ok")) and bool(worker_summary.get("ok", minute_result.get("ok")))
    summary = {
        "ok": ok,
        "source": "qmt_xtquant",
        "mode": "minute_gap_repair",
        "scenario": args.scenario,
        "start_date": args.start_date,
        "end_date": args.end_date,
        "periods": args.minute_periods,
        "minute": {
            **minute_result,
            "report_dir": str(report_dir),
            "report_path": str(summary_file),
            "worker_summary": worker_summary,
        },
        "source_empty_retries": source_empty_retries,
        "validation_status": "passed" if ok else "failed",
    }
    out_report = Path(args.report) if args.report else report_dir / "collector_summary.json"
    out_report.parent.mkdir(parents=True, exist_ok=True)
    out_report.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    summary["report_path"] = str(out_report)
    log("summary " + json.dumps({k: summary[k] for k in ["ok", "source", "mode", "scenario", "validation_status", "report_path"]}, ensure_ascii=False))
    return summary


def _read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"parse_error": str(exc)}


def _after_close_source_empty_codes(trade_dates: list[str]) -> dict[str, list[str]]:
    """Load persisted QMT 5m-empty source candidates for a one-time retry."""
    retries: dict[str, list[str]] = {}
    for trade_date in trade_dates:
        report = runtime_path(
            f"qmt_xtquant_collector_after-close_{trade_date}_{trade_date}",
            "minute_full_refresh.json",
        )
        payload = _read_json_file(report)
        periods = payload.get("summary", {}).get("periods", {})
        codes = sorted(
            {
                str(code).strip().upper()
                for code in periods.get("5m", {}).get("empty_code_list", [])
                if str(code).strip()
            }
        )
        if codes:
            retries[trade_date] = codes
    return retries


def _load_trade_dates(start_date: str, end_date: str) -> list[str]:
    rows = ch_client().query(
        "SELECT trade_date FROM trade_calendar "
        "WHERE market = 'SH' AND is_trading = 1 "
        f"AND trade_date >= toDate('{start_date}') AND trade_date <= toDate('{end_date}') "
        "ORDER BY trade_date"
    ).result_rows
    return [str(row[0]) for row in rows]


def _retry_after_close_source_empty_codes(
    args: argparse.Namespace, report_dir: Path, trade_dates: list[str]
) -> list[dict[str, Any]]:
    """Retry persisted QMT-empty 5m candidates without changing final-gap semantics."""
    results: list[dict[str, Any]] = []
    for trade_date, codes in _after_close_source_empty_codes(trade_dates).items():
        report = report_dir / "source_empty_retries" / f"retry_{trade_date.replace('-', '')}.json"
        cmd = [
            sys.executable,
            str(REPO_ROOT / "scripts" / "qmt_xtquant_minute_backfill_validate.py"),
            "--phase", "all",
            "--start-date", trade_date,
            "--end-date", trade_date,
            "--codes", ",".join(codes),
            "--periods", args.minute_periods,
            "--batch-size", str(args.minute_batch_size),
            "--batch-timeout-sec", str(args.minute_batch_timeout_sec),
            "--max-retries", str(args.max_retries),
            "--retry-sleep", str(args.retry_sleep),
            "--reset-stage",
            "--reset-stage-codes-only",
            "--repair-incomplete-cache",
            "--report", str(report),
        ]
        log(f"retry after-close QMT-empty 5m sources date={trade_date} codes={len(codes)}")
        results.append(
            {
                "trade_date": trade_date,
                "candidate_codes": codes,
                "candidate_count": len(codes),
                **_run_subprocess(cmd, timeout=args.minute_chunk_timeout_sec),
                "report_path": str(report),
            }
        )
    return results


def run_after_close_full_refresh(args: argparse.Namespace) -> dict[str, Any]:
    """Close one trading day from QMT's batch-downloaded local cache.

    The intraday whole-quote collector remains the primary low-latency source.
    This after-close path is intentionally independent: it batch-downloads the
    official 1d and 5m history first, then reads MiniQMT's local cache and
    applies the complete day before a final gap audit.
    """
    report_dir = Path(args.minute_report_dir) if args.minute_report_dir else report_path(
        "qmt_xtquant_data_source_task",
        f"after_close_full_refresh_{args.start_date}_{args.end_date}_{datetime.now(ZoneInfo('Asia/Shanghai')):%Y%m%d_%H%M%S}",
    )
    report_dir.mkdir(parents=True, exist_ok=True)
    daily_report = report_dir / "daily_full_refresh.json"
    minute_report = report_dir / "minute_full_refresh.json"

    daily_cmd = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "qmtmini_daily_backfill_validate.py"),
        "--phase", "all",
        "--start-date", args.start_date,
        "--end-date", args.end_date,
        "--batch-size", str(args.daily_batch_size),
        "--max-retries", str(args.max_retries),
        "--retry-sleep", str(args.retry_sleep),
        "--reset-stage",
        "--report", str(daily_report),
    ]
    minute_cmd = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "qmt_xtquant_minute_backfill_validate.py"),
        "--phase", "all",
        "--start-date", args.start_date,
        "--end-date", args.end_date,
        "--periods", args.minute_periods,
        "--batch-size", str(args.minute_batch_size),
        "--batch-timeout-sec", str(args.minute_batch_timeout_sec),
        "--max-retries", str(args.max_retries),
        "--retry-sleep", str(args.retry_sleep),
        "--reset-stage",
        "--in-process",
        "--report", str(minute_report),
    ]
    if "index" in {item.strip().lower() for item in args.universe.split(",")}:
        daily_cmd.append("--include-index")
        minute_cmd.append("--include-index")
    if args.codes:
        daily_cmd.extend(["--codes", args.codes])
        minute_cmd.extend(["--codes", args.codes])

    log(
        "run QMT after-close full refresh "
        + json.dumps(
            {
                "date": f"{args.start_date}~{args.end_date}",
                "daily_batch_size": args.daily_batch_size,
                "minute_batch_size": args.minute_batch_size,
                "report_dir": str(report_dir),
            },
            ensure_ascii=False,
        )
    )
    from services.operations.ingestion_checkpoint import run_staged
    daily_result = run_staged(daily_cmd, args.daily_timeout_sec, report_dir, _run_subprocess,
                              phases=("fetch", "validate-stage", "apply", "validate-target"))
    minute_result: dict[str, Any] = {"ok": False, "skipped": True, "reason": "daily_refresh_failed"}
    final_result: dict[str, Any] = {"ok": False, "skipped": True, "reason": "daily_or_minute_refresh_failed"}
    # Daily and minute products have independent stage ownership.
    minute_result = run_staged(minute_cmd, args.minute_timeout_sec, report_dir, _run_subprocess,
                               phases=("fetch", "validate-stage", "apply"))
    if daily_result.get("ok") and minute_result.get("ok"):
        # The bulk refresh owns the full universe.  This final repair/audit is
        # normally audit-only; if QMT leaves a small residual, the existing
        # targeted repair is retained as a narrow, auditable tail fallback.
        final_cmd = [
            sys.executable,
            str(REPO_ROOT / "scripts" / "qmt_xtquant_minute_gap_audit_repair.py"),
            "--mode", "repair",
            "--start-date", args.start_date,
            "--end-date", args.end_date,
            "--periods", args.minute_periods,
            "--universe", args.universe,
            "--audit-code-chunk-size", str(args.audit_code_chunk_size),
            "--repair-code-chunk-size", str(args.repair_code_chunk_size),
            "--repair-workers", str(args.repair_workers),
            "--repair-batch-size", str(args.minute_batch_size),
            "--batch-timeout-sec", str(args.minute_batch_timeout_sec),
            "--chunk-timeout-sec", str(args.minute_chunk_timeout_sec),
            "--max-retries", str(args.max_retries),
            "--retry-sleep", str(args.retry_sleep),
            "--report-dir", str(report_dir),
        ]
        if args.codes:
            final_cmd.extend(["--codes", args.codes])
        final_result = _run_subprocess(final_cmd, timeout=args.minute_timeout_sec)

    final_summary = _read_json_file(report_dir / "summary.json")
    final_validation = _read_json_file(report_dir / "final_validation.json")
    ok = bool(daily_result.get("ok")) and bool(minute_result.get("ok")) and bool(final_result.get("ok")) and bool(final_validation.get("closed"))
    summary = {
        "ok": ok,
        "source": "qmt_xtquant",
        "mode": "after_close_full_refresh",
        "scenario": args.scenario,
        "start_date": args.start_date,
        "end_date": args.end_date,
        "daily": {**daily_result, "report_path": str(daily_report)},
        "minute": {
            **minute_result,
            "report_path": str(minute_report),
            "worker_summary": final_summary,
        },
        "final_validation": final_validation,
        "final_repair": final_result,
        "validation_status": "passed" if ok else "failed",
    }
    out_report = report_dir / "collector_summary.json"
    out_report.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    summary["report_path"] = str(out_report)
    log("summary " + json.dumps({k: summary[k] for k in ["ok", "source", "mode", "scenario", "validation_status", "report_path"]}, ensure_ascii=False))
    return summary


def parse_args() -> argparse.Namespace:
    today = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    parser = argparse.ArgumentParser(description="QMT/xtquant data-source replacement task for AiStock.")
    parser.add_argument("--mode", choices=["date-repair", "minute-gap-repair", "after-close-full-refresh"], default="date-repair")
    parser.add_argument("--scenario", choices=["history", "intraday", "after-close", "manual"], default="manual")
    parser.add_argument("--start-date", default=today)
    parser.add_argument("--end-date", default=today)
    parser.add_argument("--codes", default="")
    parser.add_argument("--universe", default="stock,index")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--include-index", action="store_true")
    parser.add_argument("--daily-phase", choices=["fetch", "validate-stage", "apply", "validate-target", "all"], default="all")
    parser.add_argument("--daily-batch-size", type=int, default=80)
    parser.add_argument("--minute-batch-size", type=int, default=1)
    parser.add_argument("--minute-periods", default=",".join(DEFAULT_MINUTE_PERIODS))
    parser.add_argument("--minute-phase", choices=["fetch", "validate-stage", "fetch-validate", "apply", "all"], default="fetch-validate")
    parser.add_argument("--with-minutes", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--dividend-type", default="none", choices=["none", "front", "back"])
    parser.add_argument("--reset-stage", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--reset-minute-stage", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--retry-sleep", type=float, default=1.0)
    parser.add_argument("--daily-timeout-sec", type=int, default=5400)
    parser.add_argument("--minute-timeout-sec", type=int, default=7200)
    parser.add_argument("--minute-batch-timeout-sec", type=int, default=240)
    parser.add_argument("--minute-chunk-timeout-sec", type=int, default=900)
    parser.add_argument("--audit-code-chunk-size", type=int, default=400)
    parser.add_argument("--repair-code-chunk-size", type=int, default=1)
    parser.add_argument("--repair-workers", type=int, default=1)
    parser.add_argument("--repair-code-offset", type=int, default=0)
    parser.add_argument("--max-repair-codes", type=int, default=0)
    parser.add_argument("--issue-file", default="")
    parser.add_argument(
        "--retry-after-close-source-empty",
        action="store_true",
        help="Retry once in history mode the QMT-empty 5m source candidates persisted by after-close refresh.",
    )
    parser.add_argument("--minute-report-dir", default="")
    parser.add_argument("--stop-on-error", action="store_true")
    parser.add_argument("--report", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.mode == "date-repair":
        summary = run_date_repair(args)
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default))
        return 0 if summary.get("ok") else 1
    if args.mode == "minute-gap-repair":
        summary = run_minute_gap_repair(args)
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default))
        return 0 if summary.get("ok") else 1
    if args.mode == "after-close-full-refresh":
        summary = run_after_close_full_refresh(args)
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default))
        return 0 if summary.get("ok") else 1
    raise SystemExit(f"unsupported mode: {args.mode}")


if __name__ == "__main__":
    raise SystemExit(main())

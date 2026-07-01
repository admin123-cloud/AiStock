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
from utils.paths import report_path


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
        minute_report = report_dir / f"minute_{args.start_date}_{args.end_date}_{stamp}.json"
        minute_cmd = [
            sys.executable,
            str(REPO_ROOT / "scripts" / "qmt_xtquant_minute_backfill_validate.py"),
            "--phase",
            args.minute_phase,
            "--start-date",
            args.start_date,
            "--end-date",
            args.end_date,
            "--periods",
            args.minute_periods,
            "--batch-size",
            str(args.minute_batch_size),
            "--max-retries",
            str(args.max_retries),
            "--retry-sleep",
            str(args.retry_sleep),
            "--batch-timeout-sec",
            str(args.minute_batch_timeout_sec),
            "--dividend-type",
            args.dividend_type,
            "--report",
            str(minute_report),
        ]
        if args.codes:
            minute_cmd.extend(["--codes", args.codes])
        if args.limit:
            minute_cmd.extend(["--limit", str(args.limit)])
        if args.include_index:
            minute_cmd.append("--include-index")
        if args.reset_minute_stage:
            minute_cmd.append("--reset-stage")

        log(f"run QMT minute phase={args.minute_phase} periods={args.minute_periods} date={args.start_date}~{args.end_date}")
        minute_result = _run_subprocess(minute_cmd, timeout=args.minute_timeout_sec)
        minute_result["report_path"] = str(minute_report)

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


def parse_args() -> argparse.Namespace:
    today = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    parser = argparse.ArgumentParser(description="QMT/xtquant data-source replacement task for AiStock.")
    parser.add_argument("--mode", choices=["date-repair"], default="date-repair")
    parser.add_argument("--start-date", default=today)
    parser.add_argument("--end-date", default=today)
    parser.add_argument("--codes", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--include-index", action="store_true")
    parser.add_argument("--daily-phase", choices=["fetch", "validate-stage", "apply", "validate-target", "all"], default="all")
    parser.add_argument("--daily-batch-size", type=int, default=80)
    parser.add_argument("--minute-batch-size", type=int, default=30)
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
    parser.add_argument("--minute-batch-timeout-sec", type=int, default=180)
    parser.add_argument("--report", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.mode == "date-repair":
        summary = run_date_repair(args)
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default))
        return 0 if summary.get("ok") else 1
    raise SystemExit(f"unsupported mode: {args.mode}")


if __name__ == "__main__":
    raise SystemExit(main())

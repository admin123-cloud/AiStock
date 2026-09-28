"""Single-day ClickHouse K-line patrol.

Runs one bounded audit/repair/verify pass for a single target date.
Designed for daemon-style repeated invocation.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = PROJECT_ROOT / "data" / "runtime" / "kline_patrol"
DEFAULT_STATE_FILE = STATE_DIR / "state.json"
DEFAULT_EVENTS_FILE = STATE_DIR / "events.jsonl"
DEFAULT_REPORT_ROOT = PROJECT_ROOT / "reports" / "kline_patrol"
GOVERN_SCRIPT = PROJECT_ROOT / "scripts" / "govern_kline_history.py"

DEFAULT_PERIODS = "5m,15m,30m,60m,1d,1w,1mon,1q,1y"
REPAIR_PERIODS = "5m,15m,30m,60m"
AUDIT_KEYS = (
    "duplicate_rows",
    "bad_code_rows",
    "null_ohlc_rows",
    "non_positive_ohlc_rows",
    "bad_ohlc_rows",
    "duplicate_groups",
    "invalid_bar_time_rows",
)
MINUTE_PERIODS = ("5m", "15m", "30m", "60m")
CONSISTENCY_KEYS = (
    "missing_target_rows",
    "extra_target_rows",
    "incomplete_source_bucket_rows",
    "value_mismatch_rows",
)


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    report_dir: Path | None


def _today_text() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return dict(default)
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


def _lock(path: Path, stale_minutes: int) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        age = datetime.now().timestamp() - path.stat().st_mtime
        if age < stale_minutes * 60:
            return False
        path.unlink(missing_ok=True)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({"pid": os.getpid(), "started_at": datetime.now().isoformat()}))
    return True


def _unlock(path: Path) -> None:
    path.unlink(missing_ok=True)


def _run_govern(args: list[str], timeout_seconds: int) -> CommandResult:
    cmd = [sys.executable, str(GOVERN_SCRIPT), *args]
    proc = subprocess.run(
        cmd,
        cwd=str(PROJECT_ROOT),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout_seconds,
    )
    report_dir: Path | None = None
    for line in proc.stdout.splitlines():
        if line.startswith("report_dir="):
            report_dir = Path(line.split("=", 1)[1].strip())
            if not report_dir.is_absolute():
                report_dir = PROJECT_ROOT / report_dir
    return CommandResult(proc.returncode, proc.stdout, proc.stderr, report_dir)


def _load_report(report_dir: Path | None) -> dict[str, Any] | None:
    if not report_dir:
        return None
    report_path = report_dir / "report.json"
    if not report_path.exists():
        return None
    return json.loads(report_path.read_text(encoding="utf-8"))


def _sum_report(report: dict[str, Any]) -> tuple[dict[str, int], dict[str, int]]:
    audit = {key: 0 for key in AUDIT_KEYS}
    consistency = {key: 0 for key in CONSISTENCY_KEYS}
    for row in report.get("audit", []):
        for key in AUDIT_KEYS:
            audit[key] += int(row.get(key) or 0)
    for row in report.get("consistency", []):
        for key in CONSISTENCY_KEYS:
            consistency[key] += int(row.get(key) or 0)
    return audit, consistency


def _is_clean(report: dict[str, Any]) -> bool:
    audit, consistency = _sum_report(report)
    return not any(audit.values()) and not any(consistency.values())


def _all_minute_periods_missing(report: dict[str, Any]) -> tuple[bool, dict[str, int]]:
    minute_rows = {period: 0 for period in MINUTE_PERIODS}
    for row in report.get("audit", []):
        period = str(row.get("period", "")).strip()
        if period in minute_rows:
            minute_rows[period] = int(row.get("raw_rows") or 0)
    all_missing = all(value == 0 for value in minute_rows.values())
    return all_missing, minute_rows


def _auto_repair_allowed(report: dict[str, Any]) -> bool:
    audit, consistency = _sum_report(report)
    manual_audit = dict(audit)
    manual_audit["bad_ohlc_rows"] = 0
    auto_consistency_keys = {"incomplete_source_bucket_rows", "value_mismatch_rows"}
    manual_consistency = {
        key: value for key, value in consistency.items() if key not in auto_consistency_keys
    }
    return not any(manual_audit.values()) and not any(manual_consistency.values())


def _only_incomplete_source_remaining(consistency: dict[str, int]) -> bool:
    return (
        int(consistency.get("incomplete_source_bucket_rows") or 0) > 0
        and int(consistency.get("missing_target_rows") or 0) == 0
        and int(consistency.get("extra_target_rows") or 0) == 0
        and int(consistency.get("value_mismatch_rows") or 0) == 0
    )


def _build_audit_args(target_date: date, periods: str, out_root: Path) -> list[str]:
    text = target_date.strftime("%Y-%m-%d")
    return [
        "--start-date",
        text,
        "--end-date",
        text,
        "--periods",
        periods,
        "--check-derived",
        "--out-dir",
        str(out_root),
    ]


def _build_repair_args(target_date: date, out_root: Path, delete_partial_zero: bool) -> list[str]:
    text = target_date.strftime("%Y-%m-%d")
    args = [
        "--start-date",
        text,
        "--end-date",
        text,
        "--periods",
        REPAIR_PERIODS,
        "--skip-audit",
        "--repair-bad-ohlc",
        "--rebuild-derived",
        "--out-dir",
        str(out_root),
    ]
    if delete_partial_zero:
        args.append("--delete-partial-zero-source")
    return args


def _default_state(start_date: str | None) -> dict[str, Any]:
    # Continue from the last manually repaired but not fully verified window.
    next_date = start_date or "2026-01-16"
    return {
        "next_date": next_date,
        "direction": "backward",
        "last_status": "initialized",
        "last_checked_at": None,
        "last_clean_date": None,
        "last_report_dir": None,
        "last_error": None,
    }


def run_once(args: argparse.Namespace) -> dict[str, Any]:
    state_file = Path(args.state_file)
    events_file = Path(args.events_file)
    report_root = Path(args.report_root)
    state = _read_json(state_file, _default_state(args.start_date))

    target = _parse_date(args.date or state["next_date"])
    started_at = datetime.now().isoformat(timespec="seconds")
    event: dict[str, Any] = {
        "started_at": started_at,
        "target_date": target.strftime("%Y-%m-%d"),
        "periods": args.periods,
        "status": "running",
    }

    try:
        audit_cmd = _run_govern(
            _build_audit_args(target, args.periods, report_root / "governance"),
            args.timeout_seconds,
        )
        event["audit_stdout"] = audit_cmd.stdout
        event["audit_stderr"] = audit_cmd.stderr
        event["audit_returncode"] = audit_cmd.returncode
        event["audit_report_dir"] = str(audit_cmd.report_dir) if audit_cmd.report_dir else None
        report = _load_report(audit_cmd.report_dir)
        if audit_cmd.returncode != 0 or report is None:
            event["status"] = "audit_failed"
            event["error"] = "audit command failed or report missing"
            state["last_status"] = event["status"]
            state["last_checked_at"] = datetime.now().isoformat(timespec="seconds")
            state["last_report_dir"] = event.get("audit_report_dir")
            state["last_error"] = event["error"]
            return event

        audit_summary, consistency_summary = _sum_report(report)
        event["audit_summary"] = audit_summary
        event["consistency_summary"] = consistency_summary
        minute_missing, minute_rows = _all_minute_periods_missing(report)
        event["minute_raw_rows"] = minute_rows
        if minute_missing:
            event["status"] = "no_minute_data_stop"
            event["error"] = "all minute periods (5m/15m/30m/60m) have zero raw_rows"
            state["last_status"] = event["status"]
            state["last_checked_at"] = datetime.now().isoformat(timespec="seconds")
            state["last_report_dir"] = event.get("audit_report_dir")
            state["last_error"] = event["error"]
            return event

        if _is_clean(report):
            event["status"] = "clean"
        elif _auto_repair_allowed(report):
            delete_partial_zero = int(consistency_summary.get("incomplete_source_bucket_rows") or 0) > 0
            repair_cmd = _run_govern(
                _build_repair_args(target, report_root / "repairs", delete_partial_zero),
                args.timeout_seconds,
            )
            event["repair_stdout"] = repair_cmd.stdout
            event["repair_stderr"] = repair_cmd.stderr
            event["repair_returncode"] = repair_cmd.returncode
            event["repair_report_dir"] = str(repair_cmd.report_dir) if repair_cmd.report_dir else None
            if repair_cmd.returncode != 0:
                event["status"] = "repair_failed"
                state["last_status"] = event["status"]
                state["last_checked_at"] = datetime.now().isoformat(timespec="seconds")
                state["last_report_dir"] = event.get("repair_report_dir") or event.get("audit_report_dir")
                state["last_error"] = "repair command failed"
                return event

            verify_cmd = _run_govern(
                _build_audit_args(target, args.periods, report_root / "verify"),
                args.timeout_seconds,
            )
            event["verify_stdout"] = verify_cmd.stdout
            event["verify_stderr"] = verify_cmd.stderr
            event["verify_returncode"] = verify_cmd.returncode
            event["verify_report_dir"] = str(verify_cmd.report_dir) if verify_cmd.report_dir else None
            verify_report = _load_report(verify_cmd.report_dir)
            if verify_cmd.returncode == 0 and verify_report:
                verify_audit, verify_consistency = _sum_report(verify_report)
                event["verify_summary"] = {"audit": verify_audit, "consistency": verify_consistency}
                if _is_clean(verify_report):
                    event["status"] = "repaired_clean"
                elif args.ignore_incomplete_source and _only_incomplete_source_remaining(
                    verify_consistency
                ):
                    event["status"] = "repaired_clean"
                    event["tolerated_incomplete_source"] = True
            else:
                event["status"] = "verify_failed"
        else:
            event["status"] = "needs_manual"

        if event["status"] in {"clean", "repaired_clean"}:
            state["last_clean_date"] = target.strftime("%Y-%m-%d")
            if not args.date:
                state["next_date"] = (target - timedelta(days=1)).strftime("%Y-%m-%d")
        elif event["status"] == "needs_manual" and args.skip_manual and not args.date:
            if str(state.get("direction", "backward")).lower() == "backward":
                state["next_date"] = (target - timedelta(days=1)).strftime("%Y-%m-%d")
                event["needs_manual_skipped"] = True
        state["last_status"] = event["status"]
        state["last_checked_at"] = datetime.now().isoformat(timespec="seconds")
        state["last_report_dir"] = (
            event.get("verify_report_dir")
            or event.get("audit_report_dir")
            or event.get("repair_report_dir")
        )
        state["last_error"] = event.get("error")
        return event
    except subprocess.TimeoutExpired as exc:
        event["status"] = "timeout"
        event["error"] = f"command timed out after {exc.timeout}s"
        state["last_status"] = event["status"]
        state["last_checked_at"] = datetime.now().isoformat(timespec="seconds")
        state["last_error"] = event["error"]
        return event
    finally:
        event["finished_at"] = datetime.now().isoformat(timespec="seconds")
        _append_jsonl(events_file, event)
        _write_json(state_file, state)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one bounded K-line patrol step.")
    parser.add_argument("--date", help="Run a specific date without advancing the state cursor.")
    parser.add_argument("--start-date", help="Initial cursor date when state does not exist.")
    parser.add_argument("--periods", default=DEFAULT_PERIODS)
    parser.add_argument("--state-file", default=str(DEFAULT_STATE_FILE))
    parser.add_argument("--events-file", default=str(DEFAULT_EVENTS_FILE))
    parser.add_argument("--report-root", default=str(DEFAULT_REPORT_ROOT))
    parser.add_argument("--timeout-seconds", type=int, default=1500)
    parser.add_argument("--lock-stale-minutes", type=int, default=90)
    parser.add_argument(
        "--ignore-incomplete-source",
        action="store_true",
        help="Allow repaired iterations with only incomplete_source_bucket_rows remaining.",
    )
    parser.add_argument(
        "--skip-manual",
        action="store_true",
        help="Skip needs_manual dates and move cursor to previous date.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    lock_path = Path(args.state_file).with_suffix(".lock")
    if not _lock(lock_path, args.lock_stale_minutes):
        payload = {
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "status": "skipped_locked",
            "message": "another kline patrol run is still active",
        }
        _append_jsonl(Path(args.events_file), payload)
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    try:
        result = run_once(args)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0 if result.get("status") in {"clean", "repaired_clean", "skipped_locked"} else 2
    finally:
        _unlock(lock_path)


if __name__ == "__main__":
    raise SystemExit(main())

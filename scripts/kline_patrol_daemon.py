"""Run K-line patrol continuously in the background.

The daemon keeps calling `kline_patrol.py` sequentially or in parallel based on
configured `--concurrency` using the state cursor.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PATROL_SCRIPT = PROJECT_ROOT / "scripts" / "kline_patrol.py"
STATE_DIR = PROJECT_ROOT / "data" / "runtime" / "kline_patrol"
DAEMON_STATE_FILE = STATE_DIR / "daemon_state.json"
DAEMON_EVENTS_FILE = STATE_DIR / "daemon_events.jsonl"
PATROL_STATE_FILE = STATE_DIR / "state.json"

CONTINUE_STATUSES = {"clean", "repaired_clean"}


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


def _state_default() -> dict[str, Any]:
    return {
        "pid": None,
        "status": "initialized",
        "started_at": None,
        "last_heartbeat_at": None,
        "last_iteration_at": None,
        "last_target_date": None,
        "last_patrol_status": None,
        "last_error": None,
        "iterations": 0,
        "last_report_iterations": 0,
        "last_speed_report_at": None,
        "last_speed_report_iterations": 0,
    }


def _patrol_state_default() -> dict[str, Any]:
    return {
        "next_date": "2026-01-16",
        "direction": "backward",
        "last_status": "initialized",
        "last_checked_at": None,
        "last_clean_date": None,
        "last_report_dir": None,
        "last_error": None,
    }


def _extract_json(stdout: str) -> dict[str, Any] | None:
    start = stdout.find("{")
    end = stdout.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        return json.loads(stdout[start : end + 1])
    except json.JSONDecodeError:
        return None


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def _skip_manual_target_state(target_date: str | None) -> None:
    if not target_date:
        return
    patrol_state = _read_json(PATROL_STATE_FILE, _patrol_state_default())
    if str(patrol_state.get("direction", "backward")).lower() != "backward":
        return
    if patrol_state.get("next_date") != target_date:
        return
    try:
        parsed = datetime.strptime(target_date, "%Y-%m-%d").date()
    except ValueError:
        return
    patrol_state["next_date"] = (parsed - timedelta(days=1)).strftime("%Y-%m-%d")
    patrol_state["last_status"] = "needs_manual_skipped"
    patrol_state["last_checked_at"] = datetime.now().isoformat(timespec="seconds")
    patrol_state["last_error"] = "skipped by daemon auto-skip"
    _write_json(PATROL_STATE_FILE, patrol_state)


def _build_batch_dates(start_date: str, direction: str, count: int) -> list[str]:
    direction = direction.lower()
    if count <= 0:
        count = 1
    try:
        parsed = datetime.strptime(start_date, "%Y-%m-%d").date()
    except ValueError:
        parsed = datetime.now().date()
    dates: list[str] = []
    step = 1 if direction == "forward" else -1
    for i in range(count):
        dates.append((parsed + timedelta(days=step * i)).strftime("%Y-%m-%d"))
    return dates


def _run_patrol(args: argparse.Namespace) -> tuple[int, str, str, dict[str, Any] | None]:
    cmd = [
        sys.executable,
        str(PATROL_SCRIPT),
        "--timeout-seconds",
        str(args.timeout_seconds),
        "--lock-stale-minutes",
        str(args.lock_stale_minutes),
    ]
    if args.ignore_incomplete_source:
        cmd.append("--ignore-incomplete-source")
    if args.skip_manual:
        cmd.append("--skip-manual")
    proc = subprocess.run(
        cmd,
        cwd=str(PROJECT_ROOT),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=args.timeout_seconds + 300,
    )
    return proc.returncode, proc.stdout, proc.stderr, _extract_json(proc.stdout)


def _run_patrol_worker(args: argparse.Namespace, target_date: str, worker_idx: int) -> dict[str, Any]:
    worker_state_file = STATE_DIR / f"state_worker_{worker_idx}_{target_date.replace('-', '')}.json"
    cmd = [
        sys.executable,
        str(PATROL_SCRIPT),
        "--date",
        target_date,
        "--state-file",
        str(worker_state_file),
        "--lock-stale-minutes",
        str(args.lock_stale_minutes),
        "--timeout-seconds",
        str(args.timeout_seconds),
    ]
    if args.ignore_incomplete_source:
        cmd.append("--ignore-incomplete-source")
    if args.skip_manual:
        cmd.append("--skip-manual")
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=args.timeout_seconds + 300,
        )
        return {
            "target_date": target_date,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "payload": _extract_json(proc.stdout),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "target_date": target_date,
            "returncode": 2,
            "stdout": "",
            "stderr": f"command timed out after {exc.timeout}s",
            "payload": {
                "target_date": target_date,
                "status": "timeout",
                "error": f"command timed out after {exc.timeout}s",
            },
        }
    finally:
        try:
            worker_state_file.unlink(missing_ok=True)
        except Exception:
            pass


def _run_patrol_batch(
    args: argparse.Namespace,
    events_file: Path,
) -> tuple[list[dict[str, Any]], bool, int, str | None, str | None, str]:
    patrol_state = _read_json(PATROL_STATE_FILE, _patrol_state_default())
    direction = str(patrol_state.get("direction", "backward")).lower()
    if direction != "backward":
        direction = "backward"

    next_date = patrol_state.get("next_date") or _patrol_state_default()["next_date"]
    batch_dates = _build_batch_dates(str(next_date), direction, args.concurrency)
    if not batch_dates:
        return [], False, 0, None, None, "no date target"

    results_by_date: dict[str, dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(args.concurrency, len(batch_dates))) as pool:
        future_to_date = {
            pool.submit(_run_patrol_worker, args, target_date, index): target_date
            for index, target_date in enumerate(batch_dates)
        }
        for future in concurrent.futures.as_completed(future_to_date):
            target_date = future_to_date[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {
                    "target_date": target_date,
                    "returncode": 2,
                    "stdout": "",
                    "stderr": str(exc),
                    "payload": {
                        "status": "executor_failed",
                        "error": str(exc),
                        "target_date": target_date,
                    },
                }
            results_by_date[target_date] = result

    events: list[dict[str, Any]] = []
    can_continue = True
    last_patrol_status: str | None = None
    last_error_tail = ""

    for target_date in batch_dates:
        result = results_by_date.get(target_date, {})
        payload = result.get("payload") or {}
        payload_status = payload.get("status")
        returncode = int(result.get("returncode", 2))
        stdout = result.get("stdout", "")
        stderr = result.get("stderr", "")

        patrol_status = str(payload_status) if payload_status is not None else "failed"
        event_patrol_status = patrol_status
        event_can_continue = returncode == 0 and patrol_status in CONTINUE_STATUSES

        if args.skip_manual and patrol_status == "needs_manual":
            patrol_status = "needs_manual_skipped"
            event_patrol_status = patrol_status
            event_can_continue = True

        event = {
            "started_at": payload.get("started_at", datetime.now().isoformat(timespec="seconds")),
            "finished_at": payload.get("finished_at", datetime.now().isoformat(timespec="seconds")),
            "returncode": returncode,
            "target_date": target_date,
            "patrol_status": event_patrol_status,
            "stdout_tail": stdout[-4000:],
            "stderr_tail": stderr[-4000:],
        }
        if args.skip_manual and patrol_status == "needs_manual_skipped":
            event["needs_manual_skipped"] = True
        _append_jsonl(events_file, event)
        events.append(event)

        if not event_can_continue:
            can_continue = False
            if patrol_status != "needs_manual_skipped":
                last_error_tail = stderr[-4000:]

        last_patrol_status = patrol_status

    if can_continue:
        parsed_last = datetime.strptime(batch_dates[-1], "%Y-%m-%d").date()
        patrol_state["next_date"] = (parsed_last - timedelta(days=1)).strftime("%Y-%m-%d")
        patrol_state["last_status"] = last_patrol_status or "clean"
        patrol_state["last_checked_at"] = datetime.now().isoformat(timespec="seconds")
        patrol_state["last_error"] = None
        if last_patrol_status in CONTINUE_STATUSES or last_patrol_status == "needs_manual_skipped":
            patrol_state["last_clean_date"] = batch_dates[0]
        _write_json(PATROL_STATE_FILE, patrol_state)
        return (
            events,
            True,
            len(events),
            events[-1].get("target_date"),
            last_patrol_status,
            "",
        )

    patrol_state["last_status"] = last_patrol_status or "failed"
    patrol_state["last_checked_at"] = datetime.now().isoformat(timespec="seconds")
    patrol_state["last_error"] = last_error_tail or "batch worker failed"
    _write_json(PATROL_STATE_FILE, patrol_state)
    return (
        events,
        False,
        len(events),
        events[-1].get("target_date"),
        last_patrol_status,
        last_error_tail,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Continuously run K-line patrol until it needs attention.")
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=900,
        help="Single-iteration timeout in seconds (for one target date in kline_patrol.py).",
    )
    parser.add_argument("--lock-stale-minutes", type=int, default=90)
    parser.add_argument(
        "--sleep-seconds",
        type=int,
        default=0,
        help="Pause between kline_patrol iterations in seconds. 0 means continuous.",
    )
    parser.add_argument("--max-iterations", type=int, default=0, help="0 means unlimited.")
    parser.add_argument(
        "--ignore-incomplete-source",
        action="store_true",
        help="Treat incomplete_source_bucket_rows-only verify anomalies as repaired_clean.",
    )
    parser.add_argument("--skip-manual", action="store_true", help="Skip needs_manual dates and advance patrol cursor.")
    parser.add_argument("--state-file", default=str(DAEMON_STATE_FILE))
    parser.add_argument("--events-file", default=str(DAEMON_EVENTS_FILE))
    parser.add_argument(
        "--report-every-days",
        type=int,
        default=10,
        help="Emit a consolidated progress report every N processed dates.",
    )
    parser.add_argument(
        "--speed-report-every-minutes",
        type=int,
        default=20,
        help="Emit a speed snapshot report every N minutes.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=8,
        help="Process multiple target dates in parallel in daemon loop (1 means sequential).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    state_file = Path(args.state_file)
    events_file = Path(args.events_file)
    state = _read_json(state_file, _state_default())
    state.update(
        {
            "pid": os.getpid(),
            "status": "running",
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "last_heartbeat_at": datetime.now().isoformat(timespec="seconds"),
            "last_error": None,
            "iterations": 0,
            "last_speed_report_at": state.get("last_speed_report_at"),
            "last_speed_report_iterations": int(state.get("last_speed_report_iterations", 0) or 0),
        }
    )
    if state.get("last_speed_report_at") is None:
        state["last_speed_report_at"] = datetime.now().isoformat(timespec="seconds")
    _write_json(state_file, state)

    concurrency = max(1, int(args.concurrency))
    args.concurrency = concurrency

    while True:
        if args.max_iterations and state["iterations"] >= args.max_iterations:
            state["status"] = "max_iterations_reached"
            state["last_heartbeat_at"] = datetime.now().isoformat(timespec="seconds")
            _write_json(state_file, state)
            return 0

        started_at = datetime.now().isoformat(timespec="seconds")

        if args.concurrency <= 1:
            try:
                returncode, stdout, stderr, payload = _run_patrol(args)
            except subprocess.TimeoutExpired as exc:
                event = {
                    "started_at": started_at,
                    "finished_at": datetime.now().isoformat(timespec="seconds"),
                    "status": "timeout",
                    "error": f"patrol timed out after {exc.timeout}s",
                }
                _append_jsonl(events_file, event)
                state.update(
                    {
                        "status": "timeout",
                        "last_heartbeat_at": event["finished_at"],
                        "last_iteration_at": event["finished_at"],
                        "last_error": event["error"],
                    }
                )
                _write_json(state_file, state)
                return 2

            finished_at = datetime.now().isoformat(timespec="seconds")
            patrol_status = payload.get("status") if payload else None
            target_date = payload.get("target_date") if payload else None
            event = {
                "started_at": started_at,
                "finished_at": finished_at,
                "returncode": returncode,
                "target_date": target_date,
                "patrol_status": patrol_status,
                "stdout_tail": stdout[-4000:],
                "stderr_tail": stderr[-4000:],
            }
            _append_jsonl(events_file, event)

            continue_running = patrol_status in CONTINUE_STATUSES
            if args.skip_manual and patrol_status == "needs_manual":
                _skip_manual_target_state(target_date)
                continue_running = True
                event["status"] = "needs_manual_skipped"

            processed = 1
            state.update(
                {
                    "status": "running" if continue_running else patrol_status,
                    "last_heartbeat_at": finished_at,
                    "last_iteration_at": finished_at,
                    "last_target_date": target_date,
                    "last_patrol_status": patrol_status,
                    "last_error": None,
                    "iterations": int(state.get("iterations") or 0) + processed,
                }
            )
            last_error_tail = stderr.strip()
        else:
            events, can_continue, processed, last_target_date, patrol_status, last_error_tail = _run_patrol_batch(
                args,
                events_file,
            )
            if processed == 0:
                can_continue = False
                patrol_status = "no_target"

            heartbeat_at = datetime.now().isoformat(timespec="seconds")
            state.update(
                {
                    "status": "running" if can_continue else (patrol_status or "failed"),
                    "last_heartbeat_at": heartbeat_at,
                    "last_iteration_at": heartbeat_at,
                    "last_target_date": last_target_date,
                    "last_patrol_status": patrol_status,
                    "last_error": None if can_continue else (last_error_tail.strip() or "batch worker failed"),
                    "iterations": int(state.get("iterations") or 0) + processed,
                }
            )
            if not can_continue:
                _write_json(state_file, state)
                return 2

        report_every_days = max(1, int(args.report_every_days))
        if state["iterations"] > 0 and state["iterations"] % report_every_days == 0:
            patrol_state = _read_json(PATROL_STATE_FILE, {})
            report_event = {
                "type": "progress_report",
                "every_days": report_every_days,
                "iterations": state["iterations"],
                "last_target_date": state.get("last_target_date"),
                "next_date": patrol_state.get("next_date"),
                "last_status": state.get("last_patrol_status"),
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "last_report_iterations": state.get("iterations", 0),
            }
            _append_jsonl(events_file, report_event)
            print(
                f"progress_report iterations={state['iterations']} target={state.get('last_target_date')} "
                f"next={patrol_state.get('next_date')}"
            )
            state["last_report_iterations"] = state["iterations"]

        speed_every = max(1, int(args.speed_report_every_minutes))
        now = datetime.now()
        last_speed_report_at = _parse_ts(state.get("last_speed_report_at")) or now
        if (now - last_speed_report_at).total_seconds() >= speed_every * 60:
            patrol_state = _read_json(PATROL_STATE_FILE, {})
            speed_delta = int(state.get("iterations", 0)) - int(state.get("last_speed_report_iterations", 0))
            patrol_state_date = patrol_state.get("last_clean_date") or patrol_state.get("next_date")
            speed_event = {
                "type": "speed_report",
                "interval_minutes": speed_every,
                "iterations_delta": speed_delta,
                "iterations_total": state.get("iterations", 0),
                "snapshot_started_at": state.get("last_speed_report_at"),
                "snapshot_target_date": patrol_state_date,
                "latest_target_date": state.get("last_target_date"),
                "timestamp": now.isoformat(timespec="seconds"),
            }
            _append_jsonl(events_file, speed_event)
            print(
                f"speed_report interval={speed_every}m processed={speed_delta} total={state.get('iterations', 0)} "
                f"latest_target={state.get('last_target_date')} "
            )
            state["last_speed_report_at"] = now.isoformat(timespec="seconds")
            state["last_speed_report_iterations"] = state.get("iterations", 0)

        _write_json(state_file, state)
        time.sleep(max(0, args.sleep_seconds))


if __name__ == "__main__":
    raise SystemExit(main())

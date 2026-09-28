"""Watchdog for kline patrol daemon.

Checks every N minutes whether patrol daemon is running and restarts it when missing.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DAEMON_SCRIPT = PROJECT_ROOT / "scripts" / "kline_patrol_daemon.py"
STATE_FILE = PROJECT_ROOT / "data" / "runtime" / "kline_patrol" / "daemon_state.json"


def _read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return __import__("json").loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _is_process_running(pid: int | None) -> bool:
    if not pid:
        return False
    if not isinstance(pid, int) or pid <= 0:
        return False

    # `tasklist` is available on Windows and stable for lightweight existence check.
    list_cmd = ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"]
    try:
        result = subprocess.run(
            list_cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except Exception:
        return False
    output = (result.stdout or "").strip()
    if not output or "No tasks are running which match the specified criteria." in output:
        return False
    if "kline_patrol_daemon.py" not in output.lower():
        # Double-check command line for exact match to avoid stale PID reuse.
        cmd_line_cmd = [
            "wmic",
            "process",
            "where",
            f"ProcessId={pid}",
            "get",
            "CommandLine",
            "/FORMAT:LIST",
        ]
        try:
            cmd_result = subprocess.run(
                cmd_line_cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=8,
            )
        except Exception:
            return True
        cmd_output = (cmd_result.stdout or "").lower()
        if "kline_patrol_daemon.py" not in cmd_output:
            return False
    return True


def _start_daemon(python_path: str, daemon_args: list[str]) -> None:
    cmd = [python_path, str(DAEMON_SCRIPT), *daemon_args]
    kwargs = {}
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    subprocess.Popen(cmd, cwd=str(PROJECT_ROOT), text=True, **kwargs)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Keep kline patrol daemon alive.")
    parser.add_argument("--interval-minutes", type=int, default=15, help="Check interval in minutes.")
    parser.add_argument("--python", default=sys.executable, help="Python executable.")
    parser.add_argument("--daemon-state-file", default=str(STATE_FILE))
    parser.add_argument(
        "--daemon-args",
        default="--timeout-seconds 1200 --lock-stale-minutes 1 --ignore-incomplete-source --skip-manual --report-every-days 10 --speed-report-every-minutes 20 --concurrency 8",
        help="Arguments passed to kline_patrol_daemon.py.",
    )
    return parser.parse_args()


def main() -> int:
    import shlex

    args = parse_args()
    interval_seconds = max(1, int(args.interval_minutes) * 60)
    state_path = Path(args.daemon_state_file)
    daemon_args = shlex.split(args.daemon_args)

    print(f"watchdog started, interval={args.interval_minutes}m, script={DAEMON_SCRIPT}", flush=True)
    while True:
        state = _read_state(state_path)
        pid = state.get("pid")
        if not isinstance(pid, int):
            pid = None

        if _is_process_running(pid):
            print(f"{datetime.now().isoformat(timespec='seconds')} alive pid={pid}", flush=True)
        else:
            print(
                f"{datetime.now().isoformat(timespec='seconds')} daemon not running; starting one now.",
                flush=True,
            )
            _start_daemon(args.python, daemon_args)
            time.sleep(2)

        time.sleep(interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())

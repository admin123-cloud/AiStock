"""Plan or execute isolated minute candidate builds for a frozen baseline.

The default mode is planning only: it validates the baseline and writes a
single 81-month x 15/30/60 preflight manifest without touching ClickHouse.
``--execute`` invokes only the ``candidate`` subcommand, which writes isolated
candidate tables; this runner never calls exchange or replace-partition.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.paths import runtime_path  # noqa: E402

PERIODS = ("15", "30", "60")
EXPECTED_MONTHS = 81
EXPECTED_ASTRA5_RUN_ID = "20260912_astra5"
EXPECTED_ASTRA5_CUTOFF = "2026-09-12T23:36:24"


def _state_path(run_id: str) -> Path:
    return _manifest_path(run_id).with_name(f"candidate_batch_{run_id}_state.json")


def _manifest_path(run_id: str) -> Path:
    if not run_id or not run_id.isascii() or not run_id.replace("_", "").isalnum():
        raise ValueError(f"unsafe run id: {run_id}")
    path = runtime_path() / "minute_migration_v2" / f"candidate_batch_{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def load_baseline(run_id: str) -> tuple[dict, list[str]]:
    path = runtime_path() / "minute_migration_v2" / f"baseline_{run_id}.json"
    if not path.exists():
        raise SystemExit(f"baseline manifest not found: {path}")
    baseline = json.loads(path.read_text(encoding="utf-8"))
    entries = baseline.get("months")
    if not isinstance(entries, list) or len(entries) != EXPECTED_MONTHS:
        raise SystemExit(f"baseline must contain exactly {EXPECTED_MONTHS} months")
    months = [str(item.get("month", "")) for item in entries]
    if len(set(months)) != EXPECTED_MONTHS:
        raise SystemExit("baseline month list contains duplicates")
    if any(len(month) != 6 or not month.isdigit() or not 1 <= int(month[4:]) <= 12 for month in months):
        raise SystemExit("baseline contains an invalid month")
    return baseline, sorted(months)


def build_plan(run_id: str, months: list[str], blocked_months: set[str] | None = None) -> list[dict]:
    blocked_months = blocked_months or set()
    return [
        {
            "period": period,
            "month": month,
            "command": [sys.executable, "scripts/minute_table_migration_v2.py", "candidate",
                         "--period", period, "--month", month, "--run-id", run_id],
            "status": "blocked_incomplete_source" if month in blocked_months else "planned",
        }
        for period in PERIODS
        for month in months
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default="20260912_astra5")
    parser.add_argument("--baseline-run-id", default=None,
                        help="source baseline id; defaults to --run-id")
    parser.add_argument("--execute", action="store_true",
                        help="build isolated candidate tables; never mutates official tables")
    parser.add_argument("--drop-existing", action="store_true",
                        help="allow rebuilding existing isolated candidate tables")
    args = parser.parse_args()
    baseline_run_id = args.baseline_run_id or args.run_id
    baseline, months = load_baseline(baseline_run_id)
    if baseline_run_id != args.run_id:
        # Give candidate subprocesses an immutable run-specific manifest while
        # retaining explicit provenance to the approved baseline.
        alias_path = runtime_path() / "minute_migration_v2" / f"baseline_{args.run_id}.json"
        if alias_path.exists():
            raise SystemExit(f"run-specific baseline already exists: {alias_path}")
        baseline = dict(baseline)
        baseline["run_id"] = args.run_id
        baseline["derived_from_run_id"] = baseline_run_id
        alias_path.write_text(json.dumps(baseline, indent=2, ensure_ascii=False), encoding="utf-8")
    if baseline_run_id == EXPECTED_ASTRA5_RUN_ID and baseline.get("cutoff") != EXPECTED_ASTRA5_CUTOFF:
        raise SystemExit(
            f"baseline cutoff mismatch: expected {EXPECTED_ASTRA5_CUTOFF}, "
            f"got {baseline.get('cutoff')}"
        )
    incomplete_months = sorted(
        str(item["month"]) for item in baseline["months"]
        if int(item.get("daily_anomaly_rows", 0) or 0) > 0
    )
    items = build_plan(args.run_id, months, set(incomplete_months))
    output = {
        "run_id": args.run_id,
        "baseline_run_id": baseline_run_id,
        "baseline_fingerprint": baseline.get("fingerprint"),
        "baseline_cutoff": baseline.get("cutoff"),
        "baseline_source_table": baseline.get("source_table"),
        "baseline_months": len(months),
        "periods": list(PERIODS),
        "candidate_count": len(items),
        "incomplete_source_months": incomplete_months,
        "execution_ready": not incomplete_months,
        "mode": "execute-isolated" if args.execute else "plan-only",
        "official_table_mutation": False,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "items": items,
    }
    if args.execute and incomplete_months:
        output["status"] = "blocked_incomplete_source"
        output["execution_note"] = "No candidate tables were touched; repair and rebaseline blocked months first."
    elif args.execute:
        for item in items:
            command = list(item["command"])
            if args.drop_existing:
                command.append("--drop-existing")
            completed = subprocess.run(command, cwd=PROJECT_ROOT, text=True,
                                       capture_output=True, check=False)
            item["returncode"] = completed.returncode
            item["stdout"] = completed.stdout[-4000:]
            item["stderr"] = completed.stderr[-4000:]
            item["status"] = "built" if completed.returncode == 0 else "failed"
            if completed.returncode:
                output["status"] = "failed"
                break
        else:
            output["status"] = "completed"
    else:
        output["status"] = "planned"
    path = _manifest_path(args.run_id)
    tmp = path.with_suffix(path.suffix + ".tmp")
    serialized = json.dumps(output, indent=2, ensure_ascii=False)
    tmp.write_text(serialized, encoding="utf-8")
    tmp.replace(path)
    state_path = _state_path(args.run_id)
    state_tmp = state_path.with_suffix(state_path.suffix + ".tmp")
    state_tmp.write_text(serialized, encoding="utf-8")
    state_tmp.replace(state_path)
    print(json.dumps({"status": output["status"], "candidate_count": len(items),
                      "manifest": str(path), "state": str(state_path),
                      "official_table_mutation": False}))
    if output["status"] == "failed":
        return 1
    if output["status"] == "blocked_incomplete_source":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

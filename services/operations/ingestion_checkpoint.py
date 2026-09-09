"""Resume isolated stage tables without re-downloading successful phases."""
import hashlib
import json
from pathlib import Path
from services.operations.health import write_snapshot
from services.operations.lifecycle import InstanceLock
from services.operations.ingestion_budget import yield_requested, deferred_result


def storage_failure(message):
    return any(x in message for x in ("CHECKSUM_DOESNT_MATCH", "Sort order of blocks violated", "CORRUPTED_DATA", "UNKNOWN_TABLE"))


def report_ok(value):
    if isinstance(value, dict):
        if value.get("ok") is False or value.get("failed_batches", 0) or value.get("nonrecoverable_error"):
            return False
        return all(report_ok(x) for x in value.values())
    if isinstance(value, list):
        return all(report_ok(x) for x in value)
    return True


def run_staged(command, timeout, root, runner, *, phases):
    root = Path(root)
    # Include worker bytes: changed algorithms must not reuse an earlier checkpoint.
    identity = json.dumps(command, ensure_ascii=False) + Path(command[1]).read_text(encoding="utf-8")
    source_root = Path(__file__).resolve().parents[2]
    for dependency in ('utils/kline_units.py', 'config/minute_units.json', 'services/operations/stage_contract.py'):
        identity += hashlib.sha256((source_root / dependency).read_bytes()).hexdigest()
    key = hashlib.sha256(identity.encode()).hexdigest()[:20]
    directory = root / "batches" / key
    directory.mkdir(parents=True, exist_ok=True)
    lock = InstanceLock(directory / "owner.lock")
    lock.acquire()
    try:
        state_path = directory / "checkpoint.json"
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {"completed": [], "batch_id": key}
        if state.get("storage_blocked"):
            return {"ok": False, "reason": "storage_blocked_requires_recovery", "checkpoint": str(state_path), "batch_id": key}
        if state.get("in_flight") or state.get("uncertain"):
            return {"ok": False, "uncertain": True, "reason": "execution_deadline_uncertain", "checkpoint": str(state_path), "batch_id": key}
        for phase in phases:
            if phase in state["completed"]:
                continue
            if yield_requested():
                return {**deferred_result(), "checkpoint": str(state_path), "completed": state["completed"]}
            cmd = list(command)
            cmd[cmd.index("--phase")+1] = phase
            for flag in ("--reset-stage", "--in-process"):
                if flag in cmd:
                    cmd.remove(flag)
            if phase == "fetch":
                cmd.append("--reset-stage")
            attempt = state.setdefault("attempts", {}).get(phase, 0) + 1
            state["attempts"][phase] = attempt
            state["in_flight"] = phase
            write_snapshot(state, state_path)
            report = directory / f"{phase}-{attempt}.json"
            cmd[cmd.index("--report")+1] = str(report)
            cmd.extend(["--stage-table", "ingest_" + key])
            try:
                result = runner(cmd, timeout=timeout)
            except Exception as exc:
                # An exception after dispatch cannot establish whether a remote write finished.
                result = {"ok": False, "uncertain": True, "stderr_tail": f"{type(exc).__name__}: {exc}"}
            try:
                payload = json.loads(report.read_text(encoding="utf-8-sig")) if report.exists() else None
            except (ValueError, OSError) as exc:
                payload = None
                result = {**result, "ok": False, "stderr_tail": f"invalid phase report: {exc}"}
            if not result.get("ok") or payload is None or not report_ok(payload):
                detail = str(result) + str(payload)
                state.update(failed_phase=phase, last_result=result,
                             in_flight=None, uncertain=bool(result.get("uncertain")),
                             storage_blocked=storage_failure(detail))
                write_snapshot(state, state_path)
                return {**result, "ok": False, "storage_blocked": state.get('storage_blocked', False),
                        "failed_phase": phase, "batch_id": key, "checkpoint": str(state_path)}
            state["completed"].append(phase)
            state.update(failed_phase=None, last_result=result, in_flight=None)
            write_snapshot(state, state_path)
        return {"ok": True, "batch_id": key, "checkpoint": str(state_path), "completed": state["completed"]}
    finally:
        lock.release()

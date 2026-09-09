"""Non-overlapping ingestion lanes with independently observable progress."""
from threading import Lock, Thread
from time import monotonic
from datetime import datetime
from zoneinfo import ZoneInfo


class IngestionLane:
    def __init__(self, name, action):
        self.name, self.action = name, action
        self.guard = Lock()
        self.thread = None
        self.state = {"name": name, "status": "idle", "runs": 0, "failures": 0}
        self.started = None

    def start(self):
        if not self.guard.acquire(blocking=False):
            return False
        self.started = monotonic()
        self.state.update(status="running", started_at=datetime.now(ZoneInfo("Asia/Shanghai")).isoformat())
        def run():
            try:
                result = self.action()
                self.state.update(status="degraded" if isinstance(result, dict) and result.get("errors") else "complete", result=result, error=None,
                                  last_success=datetime.now(ZoneInfo("Asia/Shanghai")).isoformat())
            except Exception as exc:
                self.state.update(status="failed", error=f"{type(exc).__name__}: {exc}",
                                  failures=self.state["failures"] + 1)
            finally:
                self.state.update(duration_seconds=round(monotonic()-self.started, 3),
                                  runs=self.state["runs"]+1,
                                  finished_at=datetime.now(ZoneInfo("Asia/Shanghai")).isoformat())
                self.guard.release()
        self.thread = Thread(target=run, name="ingestion-"+self.name, daemon=True)
        try:
            self.thread.start()
        except Exception:
            self.guard.release()
            raise
        return True

    def snapshot(self):
        value = dict(self.state)
        if self.guard.locked():
            value["running_seconds"] = round(monotonic()-self.started, 3)
            value["overdue"] = value["running_seconds"] > 120
        return value

    def join(self, timeout=0):
        if self.thread:
            self.thread.join(timeout)
        return not self.guard.locked()

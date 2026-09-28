"""Publish the read-only AiStock runtime health snapshot.

This is safe to run frequently.  It does not fetch data, restart services, or
place orders; it only reports whether downstream strategy/page results may be
treated as current.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.runtime_health import ArtifactRule, build_snapshot, write_snapshot
from scheduler.trading_calendar import TradingCalendar
from utils.paths import runtime_path


def _latest_after_close_validation() -> Path:
    candidates = list(runtime_path().glob("qmt_xtquant_collector_after-close_*/*final_validation.json"))
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else runtime_path("missing_final_validation.json")


def default_rules() -> list[ArtifactRule]:
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    hhmm = now.strftime("%H:%M")
    strategy_max_age = 10 * 60 if ("09:25" <= hhmm <= "11:35" or "13:00" <= hhmm <= "15:15") else 20 * 3600
    return [
        ArtifactRule("qmt_after_close_validation", _latest_after_close_validation(), 36 * 3600, True, "5m/15m/30m/60m closure", "AiStock QMT xtquant After Close Repair", defer_when_non_trading_day=True),
        ArtifactRule("daily_kline_coverage", runtime_path("daily_kline_coverage", "latest.json"), 36 * 3600, False, "daily bars against SH trading calendar", "daily_kline_coverage_maintenance", True),
        ArtifactRule("g3_strategy_summary", runtime_path("gen3_state_alpha", "latest_summary.json"), strategy_max_age, False, "G3 current candidate result", "g3_state_alpha_shadow_monitor", defer_when_non_trading_day=True),
        ArtifactRule("broker_snapshot", runtime_path("gen3_state_alpha", "broker_state.json"), 24 * 3600, False, "read-only holdings and capital snapshot", "g3_state_alpha_broker_sync at 17:30", defer_when_non_trading_day=True),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish AiStock runtime health snapshot.")
    parser.add_argument("--output", type=Path, default=runtime_path("health", "latest.json"))
    args = parser.parse_args()
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    snapshot = build_snapshot(default_rules(), now=now, non_trading_day=not TradingCalendar.is_trading_day(now))
    write_snapshot(snapshot, args.output)
    print(
        json.dumps(
            {
                "publish_status": "published",
                "status": snapshot["status"],
                "strategy_actionable": snapshot["strategy_actionable"],
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )
    # A blocked/degraded snapshot is a successful publication of a safety
    # signal.  Returning non-zero here made Task Scheduler treat routine data
    # blocking as a worker crash, which obscured the real remediation owner.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

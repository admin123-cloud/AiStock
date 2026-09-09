"""Publish the read-only AiStock runtime health snapshot.

This is safe to run frequently.  It does not fetch data, restart services, or
place orders; it only reports whether downstream strategy/page results may be
treated as current.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.operations.health import ArtifactRule, build_snapshot, write_snapshot
from scheduler.trading_calendar import TradingCalendar
from utils.paths import runtime_path


def _expected_delivery_day(deadline: str) -> str:
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    cutoff = now.date() if now.strftime("%H:%M") >= deadline else now.date() - timedelta(days=1)
    try:
        from utils.market_warehouse import clickhouse_client
        rows = clickhouse_client().query(
            "SELECT max(trade_date) FROM trade_calendar WHERE market='SH' AND is_trading=1 "
            f"AND trade_date<=toDate('{cutoff.isoformat()}') SETTINGS max_execution_time=5"
        ).result_rows
        day = str(rows[0][0])[:10]
        if day < "2000-01-01":
            raise ValueError("calendar_not_ready")
    except Exception:
        return "calendar_unavailable"
    return day


def _latest_after_close_validation() -> Path:
    # Select the delivery that is due, not whichever successful file is newest.
    day = _expected_delivery_day("18:30")
    return runtime_path(f"qmt_xtquant_collector_after-close_{day}_{day}", "final_validation.json")


def default_rules() -> list[ArtifactRule]:
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    hhmm = now.strftime("%H:%M")
    strategy_max_age = 10 * 60 if ("09:25" <= hhmm <= "11:35" or "13:00" <= hhmm <= "15:15") else 20 * 3600
    return [
        ArtifactRule("reference_metadata", runtime_path("operations", "reference_metadata.json"), 36 * 3600, False, "QMT证券池与上市日期元数据验收；不等同于行情连接故障", "QMT参考资料维护", True, defer_when_non_trading_day=True),
        ArtifactRule("qmt_after_close_validation", _latest_after_close_validation(), 36 * 3600, True, "5m/15m/30m/60m closure", "AiStock QMT xtquant After Close Repair", defer_when_non_trading_day=True),
        ArtifactRule("daily_kline_coverage", runtime_path("daily_kline_coverage", "latest.json"), 36 * 3600, False, "daily bars against SH trading calendar", "daily_kline_coverage_maintenance", True, expected_business_date=_expected_delivery_day("16:10")),
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

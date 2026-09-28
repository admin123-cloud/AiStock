from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.gen2_strategy import build_gen2_backtest_history  # noqa: E402
from api.main import app  # noqa: E402
from api.trading import (  # noqa: E402
    GEN2_STRATEGY_REFRESH_STATE_PATH,
    _configure_gen2_strategy_refresh_scheduler,
)


def main() -> None:
    route_paths = sorted(
        getattr(route, "path", "")
        for route in app.routes
        if "gen2/strategy-refresh" in getattr(route, "path", "")
    )
    refresh_status = _configure_gen2_strategy_refresh_scheduler()
    backtest = build_gen2_backtest_history("g2_alpha191_volume5_keep80_runup")
    metrics = backtest.get("metrics") or {}
    result = {
        "ok": True,
        "checks": {
            "strategy_refresh_routes": route_paths,
            "strategy_refresh_route_count": len(route_paths),
            "strategy_refresh_enabled": bool(refresh_status.get("enabled")),
            "strategy_refresh_next_run_time": refresh_status.get("next_run_time"),
            "strategy_refresh_state_path": str(GEN2_STRATEGY_REFRESH_STATE_PATH),
            "strategy_refresh_state_exists": GEN2_STRATEGY_REFRESH_STATE_PATH.exists(),
            "backtest_total_return_text": metrics.get("total_return_text"),
            "backtest_trade_count": metrics.get("trade_count"),
            "backtest_signal_count": metrics.get("signal_count"),
        },
        "status": refresh_status,
    }
    required_routes = {
        "/api/trading/gen2/strategy-refresh/status",
        "/api/trading/gen2/strategy-refresh/config",
        "/api/trading/gen2/strategy-refresh/run-once",
    }
    errors = []
    if not required_routes.issubset(set(route_paths)):
        errors.append("missing strategy-refresh API routes")
    if not refresh_status.get("enabled"):
        errors.append("strategy refresh scheduler is disabled")
    if not refresh_status.get("next_run_time"):
        errors.append("strategy refresh scheduler has no next_run_time")
    if metrics.get("total_return_text") != "331.32%":
        errors.append("G2 recovered 331.32% backtest is not the active display result")
    if errors:
        result["ok"] = False
        result["errors"] = errors
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

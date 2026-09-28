from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen2_backtest_risk_cool_dynamic_circuit import _run_dynamic  # noqa: E402


BASE = ROOT / "reports" / "gen2_breakout_buy_point_research" / "breakout_family_intraday_strength_probe" / "combo_policy_probe"
SEGMENTS = {
    "2024H2_2025Q1": ("2024-07-09", "2025-03-31"),
    "2025Q2_Q4": ("2025-04-01", "2025-12-31"),
    "2026YTD": ("2026-01-01", "2026-05-26"),
}


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, (pd.Timestamp,)):
        return obj.isoformat()
    return str(obj)


def main() -> None:
    variants = ["rtret60_or_breakbox25", "rtret70_and_breakbox35"]
    policies = ["stop_cd3_skip", "dd8_half_stop_cd3_skip"]
    rows: list[dict[str, Any]] = []
    for variant in variants:
        source = BASE / "sources" / f"{variant}.parquet"
        for policy in policies:
            for segment, (start, end) in SEGMENTS.items():
                run_dir = BASE / "segment_runs" / variant / policy / segment
                summary = _run_dynamic(source, run_dir, policy, start, end, sort_mode="trigger_time")
                rows.append(
                    {
                        "variant": variant,
                        "policy": policy,
                        "segment": segment,
                        "signals": summary["signal_count"],
                        "trades": summary["trade_count"],
                        "total_return": summary["total_return"],
                        "excess_return": summary["excess_return"],
                        "max_drawdown": summary["max_drawdown"],
                        "win_rate": summary["win_rate"],
                        "guard_days": summary.get("portfolio_guard_active_days"),
                        "half_weight_days": summary.get("half_weight_days"),
                    }
                )
    pd.DataFrame(rows).to_csv(BASE / "combo_segment_summary.csv", index=False, encoding="utf-8-sig")
    (BASE / "combo_segment_summary.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    print(json.dumps(rows, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()

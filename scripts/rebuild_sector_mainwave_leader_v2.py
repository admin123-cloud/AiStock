"""Rebuild a sector-mainwave/stock-leader research contract without membership lookahead.

The warehouse currently has a one-day sector membership snapshot.  This runner
does not silently apply that snapshot to old dates.  It records the coverage,
creates a forward snapshot-ready contract, and allows historical work only as a
sector-index timing proxy until dated membership is available.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_query_df
from utils.paths import report_path


def _query(sql: str) -> list[dict]:
    try:
        return clickhouse_query_df(sql).to_dict(orient="records")
    except Exception as exc:
        return [{"error": f"{type(exc).__name__}: {exc}"}]


def build() -> dict:
    membership = _query(
        """SELECT source, min(snapshot_date) AS min_date,
                  max(snapshot_date) AS max_date,
                  uniqExact(snapshot_date) AS snapshot_days,
                  uniqExact(canonical_code) AS stocks,
                  uniqExact(sector_code) AS sectors
           FROM source_sector_stocks GROUP BY source ORDER BY source"""
    )
    sector_prices = _query(
        """SELECT min(trade_date) AS min_date, max(trade_date) AS max_date,
                  uniqExact(code) AS sector_codes
           FROM sector_kline_daily"""
    )
    dated = [r for r in membership if r.get("source") == "tdx" and int(r.get("snapshot_days") or 0) > 1]
    temporal_membership = bool(dated)
    out = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "research_only": True,
        "strategy_id": "sector_mainwave_leader_shadow_v2",
        "membership": membership,
        "sector_index_coverage": sector_prices,
        "historical_mode": "dated_membership_required" if temporal_membership else "sector_index_proxy_only",
        "forward_mode": "enabled_after_daily_membership_snapshot",
        "contract": {
            "sector_signal": "20d_and_60d_sector_index_strength_plus_breadth",
            "leader_selection": "top_3_by_prior_15d_return_and_amount_ratio",
            "execution": "next_session_open_T_plus_1",
            "holding_days": [3, 5],
            "unfillable_policy": "skip_limit_up_or_missing_open",
            "cost_stress_bps": [100, 200, 300],
            "max_slots": 3,
            "formal_buy_signal": False,
            "auto_order_allowed": False,
        },
        "promotion_blockers": [
            "dated_historical_membership_not_available" if not temporal_membership else "none",
            "forward_snapshot_period_required",
            "T_plus_1_fill_and_limit_state_audit_required",
            "sector_cluster_and_capacity_audit_required",
        ],
    }
    out_dir = report_path("sector_mainwave_leader_rebuild_v2")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "rebuild.json").write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return out


if __name__ == "__main__":
    argparse.ArgumentParser().parse_args()
    print(json.dumps(build(), ensure_ascii=False, indent=2, default=str))

from __future__ import annotations

"""Current G3 breakout-initiation candidates: event-driven 30m confirmation."""

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_institutional_mainwave_current_v1 import (
    _build_sector_diffusion, _compute_30m_features, _index_mom60, _load_current_pool,
    _numeric, _resolve_dates, _safe_float,
)


def build_current_breakout_candidates(*, entry_date: str = "", decision_date: str = "", lookback_days: int = 90, min_amount20: float = 30000.0, top_n: int = 20) -> tuple[pd.DataFrame, dict]:
    entry, decision = _resolve_dates(entry_date, decision_date)
    args = argparse.Namespace(target_date=decision, lookback_days=lookback_days, min_amount20=min_amount20)
    pool, scan_meta = _load_current_pool(args)
    pool = _numeric(pool, ["close", "ma20", "ma60", "mom20", "mom60", "high60", "amount", "amount20", "ret1", "wave_style_score"])
    template = pool[pool.get("template_pass", pd.Series(False, index=pool.index)).fillna(False).astype(bool)].copy()
    sector = _build_sector_diffusion(template)
    if not sector.empty and "tdx_mainline_sector_name" in pool.columns:
        score_map = sector.drop_duplicates("tdx_mainline_sector_name").set_index("tdx_mainline_sector_name")["sector_diffusion_score"]
        pool["sector_diffusion_score"] = pool["tdx_mainline_sector_name"].map(score_map)
    ratio = pool["amount"] / pool["amount20"]
    raw = pool[
        pool["close"].gt(pool["ma20"]) & pool["ma20"].gt(pool["ma60"])
        & pool["mom20"].between(.08, .35) & pool["mom60"].between(.15, 1.00)
        & pool["close"].ge(pool["high60"] * .97) & pool["amount20"].ge(float(min_amount20))
        & ratio.ge(1.20) & pool["ret1"].between(.01, .06)
        & pd.to_numeric(pool.get("sector_diffusion_score"), errors="coerce").ge(65.0)
    ].copy()
    raw["amount_ratio"] = ratio.loc[raw.index]
    raw, m30_meta = _compute_30m_features(raw, entry, 30)
    index_mom60, index_meta = _index_mom60(decision)
    m30_pass = raw.get("m30_confirmed", pd.Series(False, index=raw.index)).fillna(False).astype(bool)
    index_pass = index_mom60 is not None and index_mom60 <= .05
    raw["entry_mode"] = "breakout_initiation"
    raw["route"] = "institutional_mainwave"
    raw["route_label"] = "机构主升/前高突破启动"
    raw["trade_strategy"] = "mainwave_breakout_initiation"
    raw["trade_strategy_label"] = "前高突破启动"
    raw["source_strategy_label"] = "前高突破+30m承接"
    raw["route_source"] = "mainwave_breakout_current_builder_v1"
    raw["source_quality"] = "current_rebuilt"
    raw["entry_date"] = entry; raw["decision_date"] = decision
    raw["confirm_rule"] = "breakout_prior_high + sector_diffusion>=65 + first_completed_entry_day_30m_volume_breakout_prior20_high + index_mom60<=5%"
    raw["entry_price_policy"] = "first_qualified_completed_30m_close_after_breakout"
    raw["m30_confirmed"] = m30_pass
    raw["index_mom60"] = index_mom60
    # A prior-day scan cannot substitute for an entry-day completed 30m bar.
    # Such rows remain visible, but may not enter the shadow ledger yet.
    entry_day_observable = str(entry) == str(decision)
    raw["router_eligible"] = m30_pass & bool(index_pass) & entry_day_observable
    raw["shadow_status"] = np.where(raw["router_eligible"], "confirmed_shadow_candidate", "blocked_waiting_entry_day_30m_acceptance")
    pending_reason = "breakout_waiting_entry_day_completed_30m_confirmation" if not entry_day_observable else "breakout_requires_completed_30m_confirmation_or_index_gate"
    raw["block_reason"] = np.where(raw["router_eligible"], "", pending_reason)
    raw["score"] = pd.to_numeric(raw.get("wave_style_score"), errors="coerce").fillna(0) + raw["amount_ratio"].clip(0, 4) * 5
    raw["reference_close"] = raw["close"]
    raw["auto_order_allowed"] = False; raw["formal_buy_signal"] = False; raw["order_path_enabled"] = False
    raw = raw.sort_values(["router_eligible", "sector_diffusion_score", "score"], ascending=[False, False, False]).drop_duplicates("code_raw").head(int(top_n))
    meta = {"source": "mainwave_breakout_current_builder_v1", "entry_date": entry, "decision_date": decision, "rows": int(len(raw)), "eligible_rows": int(raw["router_eligible"].sum()), "status": "shadow_only", "entry_day_observable": entry_day_observable, "index_mom60": _safe_float(index_mom60), "scan": scan_meta, "m30": m30_meta, "contract": "4% hard stop; +10% sell half; runner previous-day-low protection; entry only after the first completed 30m confirmation"}
    return raw.reset_index(drop=True), meta

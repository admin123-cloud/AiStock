"""Test whether legacy independent breakouts add value only when mainwave slots are vacant.

This is deliberately a research-only, fixed-history scheduler.  The mainwave
stream has priority; a legacy breakout can enter only when a 50% account slot
is genuinely free.  It does not claim fill parity or daily mark-to-market.
"""

from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.paths import report_path


MAINWAVE = report_path(
    "g3_recalled_mainwave_contract_v1",
    "20240101_20260630_breakout_score88_sector2_top10",
    "two_slot_closed_trades.csv",
)
BREAKOUT = report_path("breakout_1030_stop_sensitivity_v1", "closed_trades.csv")
OUT_DIR = report_path("g3_mainwave_breakout_complementarity_v1")


def metrics(frame: pd.DataFrame) -> dict:
    values = pd.to_numeric(frame.get("net_ret"), errors="coerce").dropna()
    wins, losses = values[values > 0], values[values < 0]
    return {
        "trades": int(len(values)),
        "win_rate": float((values > 0).mean()) if len(values) else None,
        "avg_net_ret": float(values.mean()) if len(values) else None,
        "avg_win": float(wins.mean()) if len(wins) else None,
        "avg_loss": float(losses.mean()) if len(losses) else None,
        "payoff_ratio": float(wins.mean() / abs(losses.mean())) if len(wins) and len(losses) else None,
        "worst_trade": float(values.min()) if len(values) else None,
    }


def main() -> int:
    mainwave = pd.read_csv(MAINWAVE, low_memory=False).copy()
    breakout = pd.read_csv(BREAKOUT, low_memory=False).copy()
    mainwave = mainwave.assign(
        route="institutional_mainwave",
        priority=0,
        entry_date=pd.to_datetime(mainwave["entry_date"]),
        exit_date=pd.to_datetime(mainwave["exit_date"]),
        net_ret=pd.to_numeric(mainwave["net_ret"], errors="coerce"),
        signal_rank=pd.to_numeric(mainwave["rank_key"], errors="coerce"),
    )
    start, end = mainwave["entry_date"].min(), mainwave["entry_date"].max()
    breakout = breakout.loc[pd.to_numeric(breakout["stop_pct"], errors="coerce").eq(0.04)].copy()
    breakout = breakout.assign(
        route="legacy_independent_breakout",
        priority=1,
        entry_date=pd.to_datetime(breakout["entry_date"]),
        exit_date=pd.to_datetime(breakout["exit_date"]),
        net_ret=pd.to_numeric(breakout["net_ret"], errors="coerce"),
        signal_rank=pd.to_numeric(breakout["signal_score"], errors="coerce"),
    )
    breakout = breakout.loc[breakout["entry_date"].between(start, end)].copy()
    breakout = breakout.dropna(subset=["entry_date", "exit_date", "net_ret", "code"])
    calendar = pd.date_range(start, max(mainwave["exit_date"].max(), breakout["exit_date"].max()), freq="B")
    mainwave_open = {
        day: int(((mainwave["entry_date"] <= day) & (mainwave["exit_date"] > day)).sum()) for day in calendar
    }
    accepted: list[dict] = [row._asdict() for row in mainwave.itertuples(index=False)]
    skipped: list[dict] = []
    breakout_open = {day: 0 for day in calendar}
    accepted_codes_by_day = {day: set(mainwave.loc[(mainwave["entry_date"] <= day) & (mainwave["exit_date"] > day), "code"]) for day in calendar}
    for row in breakout.sort_values(["entry_date", "signal_rank"], ascending=[True, False]).itertuples(index=False):
        item = row._asdict()
        held_days = pd.date_range(item["entry_date"], item["exit_date"] - pd.Timedelta(days=1), freq="B")
        held_days = [day for day in held_days if day in mainwave_open]
        if any(item["code"] in accepted_codes_by_day[day] for day in held_days):
            item["skip_reason"] = "duplicate_code_with_mainwave_or_prior_breakout"
            skipped.append(item)
        elif any(mainwave_open[day] + breakout_open[day] >= 2 for day in held_days):
            # Mainwave priority applies over the breakout's full holding
            # window, so a breakout is never allowed to displace a later
            # mainwave entry.
            item["skip_reason"] = "no_vacant_slot_for_full_holding_window"
            skipped.append(item)
        else:
            accepted.append(item)
            for day in held_days:
                breakout_open[day] += 1
                accepted_codes_by_day[day].add(item["code"])

    accepted_df = pd.DataFrame(accepted).sort_values(["entry_date", "priority", "code"])
    skipped_df = pd.DataFrame(skipped).sort_values(["entry_date", "code"])
    slot_counts = []
    for day in calendar:
        slots = mainwave_open[day] + breakout_open[day]
        slot_counts.append({"date": day, "open_slots": slots, "utilization_proxy": slots / 2.0})
    utilization = pd.DataFrame(slot_counts)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    accepted_df.to_csv(OUT_DIR / "accepted_two_slot_trades.csv", index=False, encoding="utf-8-sig")
    skipped_df.to_csv(OUT_DIR / "skipped_breakout_candidates.csv", index=False, encoding="utf-8-sig")
    utilization.to_csv(OUT_DIR / "slot_utilization_calendar_proxy.csv", index=False, encoding="utf-8-sig")
    main_util = utilization.copy()
    main_util["open_slots"] = [
        int(((mainwave["entry_date"] <= day) & (mainwave["exit_date"] > day)).sum()) for day in calendar
    ]
    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "status": "research_only_complementarity_test",
        "contract": "mainwave priority; legacy -4% breakout fills only vacant 50% slots; max two slots",
        "period": {"start": start.date().isoformat(), "end": end.date().isoformat()},
        "mainwave_baseline": metrics(mainwave),
        "legacy_breakout_candidates_in_period": metrics(breakout),
        "accepted_combined": metrics(accepted_df),
        "accepted_breakout_supplement": metrics(accepted_df[accepted_df["route"].eq("legacy_independent_breakout")]),
        "breakout_candidates": int(len(breakout)),
        "breakout_accepted": int((accepted_df["route"] == "legacy_independent_breakout").sum()),
        "breakout_skipped_capacity_or_duplicate": int(len(skipped_df)),
        "mainwave_utilization_proxy": float(main_util["open_slots"].mean() / 2.0),
        "combined_utilization_proxy": float(utilization["utilization_proxy"].mean()),
        "utilization_change": float(utilization["utilization_proxy"].mean() - main_util["open_slots"].mean() / 2.0),
        "limitations": "Fixed historical streams; old breakout lacks comparable industry fields; workday slot-occupancy proxy is not China trading-calendar or daily MTM; not executable/live evidence.",
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

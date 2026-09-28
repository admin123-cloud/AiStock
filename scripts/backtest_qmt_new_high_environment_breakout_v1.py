"""Replay the unchanged mainwave breakout contract with a prior-day NH environment gate."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scripts.backtest_g3_recalled_mainwave_contract_v1 as replay  # noqa: E402
from utils.paths import report_path  # noqa: E402


DEFAULT_BREADTH = report_path("qmt_new_high_breadth_v1", "new_high_breadth_daily.parquet")
DEFAULT_OUTPUT = report_path("qmt_new_high_environment_breakout_v1")
BASE_REPLAY = report_path(
    "g3_recalled_mainwave_contract_v1",
    "20200101_20260630_breakout_score88_sector2_stop10%_top10",
)


def run(start: str, end: str, output_dir: Path, breadth_path: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    breadth = pd.read_parquet(breadth_path)
    breadth["trade_date"] = pd.to_datetime(breadth["trade_date"]).dt.normalize()
    # The base run is a frozen, completed replay of exactly the contract being
    # audited.  Reusing its candidate/ticket ledger means this overlay changes
    # only the availability gate; it neither re-queries intraday bars nor risks
    # changing a historical 30-minute execution decision.
    candidates = pd.read_csv(BASE_REPLAY / "daily_shortlist_candidates.csv", low_memory=False)
    tickets = pd.read_csv(BASE_REPLAY / "m30_confirmed_tickets.csv", low_memory=False)
    candidates = candidates[
        (pd.to_datetime(candidates["trade_date"]) >= pd.Timestamp(start))
        & (pd.to_datetime(candidates["trade_date"]) <= pd.Timestamp(end))
    ].copy()
    tickets = tickets[
        (pd.to_datetime(tickets["trade_date"]) >= pd.Timestamp(start))
        & (pd.to_datetime(tickets["trade_date"]) <= pd.Timestamp(end))
    ].copy()
    candidates["trade_date"] = pd.to_datetime(candidates["trade_date"]).dt.normalize()
    candidates = candidates.merge(breadth[["trade_date", "breakout_environment", "nh100_breadth", "nhall_breadth"]], on="trade_date", how="left")
    candidates["breakout_environment"] = candidates["breakout_environment"].fillna(False).astype(bool)
    gated = candidates[candidates["breakout_environment"]].copy().reset_index(drop=True)
    tickets["trade_date"] = pd.to_datetime(tickets["trade_date"]).dt.normalize()
    tickets_df = tickets.merge(
        breadth[["trade_date", "breakout_environment", "nh100_breadth", "nhall_breadth"]],
        on="trade_date", how="left", suffixes=("", "_gate"),
    )
    tickets_df["breakout_environment"] = tickets_df["breakout_environment"].fillna(False).astype(bool)
    tickets_df = tickets_df[tickets_df["breakout_environment"]].copy().reset_index(drop=True)
    entered, skipped = replay._two_slot(tickets_df)
    realised_curve, realised_summary = replay._realised_proxy_curve(entered)
    funded_curve, funded_summary = replay._two_slot_funded_curve(entered)
    for frame, name in [(candidates, "all_candidates_with_environment.csv"), (gated, "environment_pass_candidates.csv"), (tickets_df, "gated_tickets.csv"), (entered, "gated_closed_trades.csv"), (skipped, "slot_skipped_tickets.csv"), (realised_curve, "gated_realised_proxy_curve.csv"), (funded_curve, "gated_funded_equity_curve.csv")]:
        frame.to_csv(output_dir / name, index=False, encoding="utf-8-sig")
    payload = {
        "research_only": True,
        "contract": "Unchanged score88/sector2/top10/30m breakout/two-slot contract; only new element is the prior signal-date QMT NH environment gate.",
        "visible_at_entry": "breakout_environment is measured from candidate trade_date (the trading day before entry_date).",
        "candidates_before_gate": int(len(candidates)),
        "candidates_after_gate": int(len(gated)),
        "candidate_retention": float(len(gated) / len(candidates)) if len(candidates) else None,
        "tickets": int(len(tickets_df)),
        "skipped": int(len(skipped)),
        "two_slot": replay._summary(entered), **realised_summary, **funded_summary,
        "promotion": "no_promote_until_same-contract window and prebreakout-layer comparison complete",
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay breakout with QMT new-high environment")
    parser.add_argument("--start-date", default="2020-01-01")
    parser.add_argument("--end-date", default="2026-06-30")
    parser.add_argument("--breadth", default=str(DEFAULT_BREADTH))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()
    print(json.dumps(run(args.start_date, args.end_date, Path(args.output_dir), Path(args.breadth)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

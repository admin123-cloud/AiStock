"""Audit the two legs of a QMT-NH-gated pre-breakout entry.

This is deliberately a per-signal diagnostic, before a portfolio/two-slot
implementation is allowed.  The early leg is executable at the next session
open from prior-day data; its fixed 20-session close is a transparent proxy,
not a claim that it shares the production 30-minute exit implementation.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scripts.backtest_g3_recalled_mainwave_contract_v1 as replay  # noqa: E402
from utils.paths import report_path  # noqa: E402


BASE = report_path("g3_recalled_mainwave_contract_v1", "20200101_20260630_breakout_score88_sector2_stop10%_top10")
BREADTH = report_path("qmt_new_high_breadth_v1", "new_high_breadth_daily.parquet")
OUT = report_path("qmt_prebreak_layer_legs_audit_v1")


def stats(frame: pd.DataFrame, column: str) -> dict:
    ret = pd.to_numeric(frame.get(column), errors="coerce").dropna()
    return {
        "signals": int(len(frame)), "observations": int(len(ret)),
        "win_rate": float((ret > 0).mean()) if len(ret) else None,
        "avg_ret": float(ret.mean()) if len(ret) else None,
        "median_ret": float(ret.median()) if len(ret) else None,
        "worst_ret": float(ret.min()) if len(ret) else None,
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    candidates = pd.read_csv(BASE / "daily_shortlist_candidates.csv", low_memory=False)
    tickets = pd.read_csv(BASE / "m30_confirmed_tickets.csv", low_memory=False)
    breadth = pd.read_parquet(BREADTH)
    for frame in (candidates, tickets, breadth):
        frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.normalize()

    candidates = candidates.merge(
        breadth[["trade_date", "breakout_environment", "nh100_breadth", "nhall_breadth"]],
        on="trade_date", how="left",
    )
    candidates["breakout_environment"] = candidates["breakout_environment"].fillna(False).astype(bool)
    gated = candidates[candidates["breakout_environment"]].copy()

    ticket_cols = ["code_raw", "trade_date", "entry_datetime", "entry_price", "exit_date", "net_ret", "exit_reason"]
    tickets = tickets[ticket_cols].rename(columns={"net_ret": "breakout_net_ret", "exit_date": "breakout_exit_date"})
    legs = gated.merge(tickets, on=["code_raw", "trade_date"], how="left")
    # This return was computed from the signal-day candidate ledger: next open
    # to the close after 20 completed trading sessions.  Charge the same 30 bps
    # entry/exit proxy once; no intraday stop is invented for this early leg.
    legs["prebreak_net_ret_h20_proxy"] = pd.to_numeric(legs["gross_ret_nextopen_h20"], errors="coerce") - replay.FEE
    legs["breakout_confirmed"] = legs["breakout_net_ret"].notna()
    # 50% prebreak, then another 50% only after the original breakout ticket.
    legs["staged_signal_return_proxy"] = 0.5 * legs["prebreak_net_ret_h20_proxy"] + 0.5 * legs["breakout_net_ret"].fillna(0.0)
    legs.to_csv(OUT / "gated_prebreak_leg_ledger.csv", index=False, encoding="utf-8-sig")

    confirmed = legs[legs["breakout_confirmed"]].copy()
    payload = {
        "research_only": True,
        "contract": "Existing score88/sector2/top10 daily shortlist; prior signal-date QMT NH environment; prebreak leg at next open, 20-session close proxy; second 50% only on unchanged 30m breakout confirmation.",
        "timing": "Environment is known after signal-date close. The prebreak leg can first enter at the next trading-session open.",
        "non_claim": "This is leg-level evidence only. It is not yet a two-slot, capital-aware staged portfolio replay and does not claim a production intraday stop for the prebreak leg.",
        "gated_prebreak_leg": stats(legs, "prebreak_net_ret_h20_proxy"),
        "of_which_later_breakout_confirmed": stats(confirmed, "prebreak_net_ret_h20_proxy"),
        "later_breakout_leg": stats(confirmed, "breakout_net_ret"),
        "two_leg_signal_proxy": stats(legs, "staged_signal_return_proxy"),
        "breakout_confirmation_rate": float(legs["breakout_confirmed"].mean()) if len(legs) else None,
        "promotion": "no_promote_until_box-structure definition, early-leg stop/exit, and two-slot capital-aware replay pass independent windows",
    }
    (OUT / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

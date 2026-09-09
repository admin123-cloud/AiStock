"""Execution and cooldown stress test for the G3 weak-rebound-veto candidate.

This is intentionally a conservative ticket-level audit: it does not claim to
model real limit-up/limit-down fills, only adds explicit adverse assumptions.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.audit_g3_mainwave_market_state_v1 as market
import scripts.backtest_g3_recalled_mainwave_contract_v1 as replay
from utils.paths import report_path


OUT_DIR = report_path("g3_mainwave_execution_stress_v1")
WINDOWS = market.WINDOWS


def _two_slots_with_cooldown(tickets: pd.DataFrame, cooldown_days: int = 3) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Same entry-time two-slot competition, plus pause after two closed losses."""
    work = tickets.copy()
    work["entry_ts"] = pd.to_datetime(work["entry_datetime"], errors="coerce")
    work["entry_date_ts"] = work["entry_ts"].dt.normalize()
    work["exit_date_ts"] = pd.to_datetime(work["exit_date"], errors="coerce").dt.normalize()
    work = work.dropna(subset=["entry_ts", "entry_date_ts", "exit_date_ts"]).sort_values(
        ["entry_ts", "daily_rank", "rank_key"], ascending=[True, True, False]
    )
    calendar = list(replay.base._trade_calendar(work["entry_date_ts"].min(), work["exit_date_ts"].max()))
    index_of = {pd.Timestamp(day).normalize(): idx for idx, day in enumerate(calendar)}
    active, entered, skipped, closed_losses = [], [], [], 0
    paused_until: pd.Timestamp | None = None
    for ticket in work.to_dict("records"):
        day = ticket["entry_date_ts"]
        closed = sorted(
            (position for position in active if position["exit_date_ts"] < day),
            key=lambda position: position["exit_date_ts"],
        )
        active = [position for position in active if position["exit_date_ts"] >= day]
        for position in closed:
            closed_losses = closed_losses + 1 if float(position["net_ret"]) <= 0 else 0
            if closed_losses >= 2:
                pos = index_of.get(pd.Timestamp(position["exit_date_ts"]).normalize())
                if pos is not None:
                    resume = min(pos + cooldown_days + 1, len(calendar) - 1)
                    paused_until = pd.Timestamp(calendar[resume]).normalize()
                closed_losses = 0
        if paused_until is not None and day < paused_until:
            ticket["slot_decision"] = "skipped_cooldown_after_two_losses"
            skipped.append(ticket)
        elif len(active) >= 2:
            ticket["slot_decision"] = "skipped_slots_occupied"
            skipped.append(ticket)
        else:
            ticket["slot_decision"] = "entered_slot"
            ticket["slot_id"] = len(active) + 1
            entered.append(ticket)
            active.append(ticket)
    return pd.DataFrame(entered), pd.DataFrame(skipped)


def _apply_execution_stress(trades: pd.DataFrame, round_trip_cost: float, stop_gap: float) -> pd.DataFrame:
    stressed = trades.copy()
    stressed["base_net_ret"] = pd.to_numeric(stressed["net_ret"], errors="coerce")
    stop = stressed["exit_reason"].eq("hard_stop")
    stressed["execution_penalty"] = round_trip_cost + stop.astype(float) * stop_gap
    stressed["net_ret"] = stressed["base_net_ret"] - stressed["execution_penalty"]
    return stressed


def _metrics(trades: pd.DataFrame) -> dict:
    summary = replay._summary(trades)
    _, funded = replay._two_slot_funded_curve(trades)
    return {
        **summary, **funded,
        "hard_stop_rate": float(trades["exit_reason"].eq("hard_stop").mean()) if len(trades) else None,
    }


def _window_rows(name: str, trades: pd.DataFrame) -> list[dict]:
    rows = []
    for window, (start, end) in WINDOWS.items():
        part = trades[trades["entry_date_ts"].between(pd.Timestamp(start), pd.Timestamp(end))]
        rows.append({"variant": name, "window": window, "trades": len(part), **_metrics(part)})
    return rows


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tickets = market._tickets()
    below_ma20 = pd.to_numeric(tickets["index_close"], errors="coerce") < pd.to_numeric(tickets["index_ma20"], errors="coerce")
    mom60_positive = pd.to_numeric(tickets["index_mom60"], errors="coerce") > 0
    veto_tickets = tickets[~(below_ma20 & mom60_positive)].copy()
    base, _ = replay._two_slot(tickets)
    veto, _ = replay._two_slot(veto_tickets)
    cooldown, cooldown_skipped = _two_slots_with_cooldown(veto_tickets)
    stress = _apply_execution_stress(veto, round_trip_cost=0.005, stop_gap=0.02)
    cooldown_stress = _apply_execution_stress(cooldown, round_trip_cost=0.005, stop_gap=0.02)
    variants = {
        "base_current": base,
        "weak_rebound_veto": veto,
        "veto_plus_2loss_3day_cooldown": cooldown,
        "veto_execution_stress_50bps_plus_stop_gap2pct": stress,
        "veto_cooldown_execution_stress": cooldown_stress,
    }
    rows = [row for name, trades in variants.items() for row in _window_rows(name, trades)]
    metrics = pd.DataFrame(rows)
    metrics.to_csv(OUT_DIR / "window_metrics.csv", index=False, encoding="utf-8-sig")
    cooldown_skipped.to_csv(OUT_DIR / "cooldown_skipped_tickets.csv", index=False, encoding="utf-8-sig")
    for name, trades in variants.items():
        trades.to_csv(OUT_DIR / f"{name}_trades.csv", index=False, encoding="utf-8-sig")
    payload = {
        "status": "completed", "research_only": True, "generated_at": datetime.now().isoformat(timespec="seconds"),
        "base_contract": "same reconstructed 30m-confirmed tickets and two 50% slots",
        "candidate_state_veto": "block only index below MA20 AND index_mom60 > 0",
        "cooldown": "after two sequential closed losses, skip new entries for the next 3 trading days",
        "execution_stress": "subtract 50 bps round-trip from every trade and another 2% from hard-stop trades",
        "cooldown_skipped_tickets": int(len(cooldown_skipped[cooldown_skipped["slot_decision"].eq("skipped_cooldown_after_two_losses")])),
        "limitations": "The stop-gap penalty is a sensitivity assumption, not a historical limit-price fill reconstruction; no tick-level fill or liquidity model.",
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# G3机构主升弱反弹否决：执行压力与冷却审计 v1", "", "- 仅研究，不改运行合同、影子账本或下单链路。",
             "- 压力口径：每笔额外扣 50bp 往返成本；硬止损再扣 2%，用于代理跳空/跌停无法按理论止损成交的风险。", "",
             "## 分窗口结果", "", metrics.to_markdown(index=False), ""]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

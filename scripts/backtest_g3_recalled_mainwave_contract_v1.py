"""Research-only two-slot replay built from the recovered mainwave skeleton.

Selection is signal-date-only: shape score >= 88, QMT canonical 000001.SH
index_mom60 <= 5%, no learned-sector or historical scheduler labels.  A daily
candidate becomes executable only at its first completed 30-minute acceptance.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.backtest_wave_style_template_strategy_v1 as base
from utils.market_warehouse import clickhouse_query_df
from utils.paths import report_path


OUT_ROOT = report_path("g3_recalled_mainwave_contract_v1")
INDEX_CODE = "000001.SH"
FEE = 0.003


def _num(value: Any, default: float = 0.0) -> float:
    try:
        value = float(value)
        return value if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def _daily_candidates(
    start: str, end: str, daily_candidate_limit: int, min_wave_score: float, min_sector_signal_count: int
) -> tuple[pd.DataFrame, pd.DataFrame]:
    base.INDEX_CODE = INDEX_CODE
    base.INDEX_FALLBACK_CODES = []
    ns = argparse.Namespace(start_date=start, end_date=end, min_score=88.0, strict_min_score=100.0, cost_bps=30.0)
    load_start = (pd.Timestamp(start) - pd.Timedelta(days=300)).strftime("%Y-%m-%d")
    load_end = (pd.Timestamp(end) + pd.Timedelta(days=90)).strftime("%Y-%m-%d")
    stocks = base._load_stocks()
    raw = base._load_daily(load_start, load_end).merge(stocks[["code_key", "stock_name", "industry"]], on="code_key", how="inner")
    features = base._attach_sector(base._add_features(raw, max_hold=20))
    index = base._load_index_features(load_start, load_end)
    if index.empty or set(index.get("index_code", pd.Series(dtype=str)).dropna()) - {INDEX_CODE}:
        raise RuntimeError(f"canonical index features unavailable: {INDEX_CODE}")
    features = features.merge(index, on="trade_date", how="left")
    candidates = base._build_signal_candidates(features, ns, 20, use_learned_sector=False, strict=False, market_gate="none")
    candidates = candidates[pd.to_numeric(candidates["index_mom60"], errors="coerce").le(0.05)].copy()
    candidates = candidates[pd.to_numeric(candidates["wave_style_score"], errors="coerce").ge(min_wave_score)].copy()
    sector_keys = ["entry_date", "l2_sector_code"]
    valid_sector = candidates["l2_sector_code"].fillna("").astype(str).str.len().gt(0)
    diffusion = (
        candidates.loc[valid_sector]
        .groupby(sector_keys)["code_key"]
        .nunique()
        .rename("sector_signal_count")
        .reset_index()
    )
    candidates = candidates.merge(diffusion, on=sector_keys, how="left")
    candidates["sector_signal_count"] = candidates["sector_signal_count"].fillna(0).astype(int)
    candidates = candidates[candidates["sector_signal_count"].ge(min_sector_signal_count)].copy()
    candidates = candidates.sort_values(["entry_date", "rank_key", "amount_rank"], ascending=[True, False, False])
    candidates["daily_rank"] = candidates.groupby("entry_date").cumcount() + 1
    shortlist = candidates[candidates["daily_rank"].le(daily_candidate_limit)].copy().reset_index(drop=True)
    # Persist the exact frozen-contract decisions with the research artifact.
    # These labels make the historical/realtime boundary auditable; the account
    # gate deliberately remains "not_modelled" and must never be treated as a
    # live-account pass.
    shortlist["strategy_id"] = "g3_institutional_mainwave_score88_v1"
    shortlist["contract"] = "g3_institutional_mainwave_score88_v1"
    shortlist["router_eligible"] = True
    shortlist["m30_confirmed"] = False
    shortlist["source_quality"] = "qmt_canonical_research_ohlc_proxy"
    shortlist["account_risk_action"] = "not_modelled_research_only"
    daily = raw.copy()
    daily["trade_date"] = pd.to_datetime(daily["trade_date"], errors="coerce").dt.normalize()
    return shortlist, daily


def _load_m30_for_candidate_days(candidates: pd.DataFrame, chunk_days: int = 80) -> dict[str, pd.DataFrame]:
    """Fetch only each candidate-day's codes and lookback window.

    Do not query every selected code across a whole calendar chunk: that turns
    a daily top-N shortlist into a large code-by-date cross product and has no
    effect on the first-30m-confirmation calculation.
    """
    if candidates.empty:
        return {}
    days = sorted(pd.to_datetime(candidates["entry_date"]).dt.normalize().unique())
    parts: list[pd.DataFrame] = []
    for offset in range(0, len(days), chunk_days):
        block = set(days[offset : offset + chunk_days])
        pick = candidates[pd.to_datetime(candidates["entry_date"]).dt.normalize().isin(block)]
        clauses: list[str] = []
        for day, day_rows in pick.groupby(pd.to_datetime(pick["entry_date"], errors="coerce").dt.normalize()):
            if pd.isna(day):
                continue
            codes = day_rows["code_raw"].dropna().astype(str).unique().tolist()
            if not codes:
                continue
            quoted = ",".join("'" + code.replace("'", "''") + "'" for code in codes)
            start = (pd.Timestamp(day) - pd.Timedelta(days=7)).strftime("%Y-%m-%d")
            end = pd.Timestamp(day).strftime("%Y-%m-%d")
            clauses.append(f"(code IN ({quoted}) AND toDate(datetime) BETWEEN toDate('{start}') AND toDate('{end}'))")
        if not clauses:
            continue
        frame = clickhouse_query_df(
            f"SELECT code,datetime,open,high,low,close,volume FROM kline_minute_30 "
            f"WHERE {' OR '.join(clauses)} "
            "ORDER BY code,datetime"
        )
        if not frame.empty:
            parts.append(frame)
    bars = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if bars.empty:
        return {}
    bars["datetime"] = pd.to_datetime(bars["datetime"], errors="coerce")
    # This ClickHouse minute table labels legacy QMT session bars eight hours
    # ahead (for example, the 10:00 A-share bar is returned as 18:00+08).
    # Convert aware values to the market's actual wall-clock session before
    # applying the 09:30-15:00 completed-bar contract.  Older naive archives
    # already carry wall-clock values and are left unchanged.
    if getattr(bars["datetime"].dt, "tz", None) is not None:
        bars["datetime"] = bars["datetime"].dt.tz_convert("Asia/Shanghai").dt.tz_localize(None) - pd.Timedelta(hours=8)
    bars = bars.dropna(subset=["datetime"]).copy()
    for column in ["open", "high", "low", "close", "volume"]:
        bars[column] = pd.to_numeric(bars[column], errors="coerce")
    bars["date"] = bars["datetime"].dt.normalize()
    bars["time"] = bars["datetime"].dt.strftime("%H:%M:%S")
    return {str(code): group.sort_values("datetime").reset_index(drop=True) for code, group in bars.groupby("code")}


def _first_acceptance(row: pd.Series, bars: pd.DataFrame, mode: str) -> tuple[float, pd.Timestamp] | None:
    entry_day = pd.Timestamp(row["entry_date"]).normalize()
    # The acceptance test only references the preceding 20 completed bars.
    # Keeping 60 bars through the entry session is mathematically equivalent
    # to scanning the whole history but avoids an O(candidates * history)
    # replay on multi-year data.
    history = bars[bars["datetime"] <= entry_day + pd.Timedelta(hours=15)].tail(60).copy()
    session = history[(history["date"].eq(entry_day)) & (history["time"] > "09:30:00")]
    for idx, bar in session.iterrows():
        previous = history[history["datetime"] < bar["datetime"]].tail(20)
        if len(previous) != 20:
            continue
        ma_ok = _num(bar["close"]) >= _num(previous["close"].mean())
        if mode == "ma20":
            accepted = ma_ok
        elif mode == "breakout":
            accepted = (
                ma_ok
                and _num(bar["close"]) >= _num(previous["high"].max())
                and _num(bar["close"]) >= _num(bar["open"])
                and _num(bar["volume"]) >= _num(previous["volume"].mean()) * 1.20
            )
        else:
            raise ValueError(f"unknown acceptance mode: {mode}")
        if accepted:
            return _num(bar["close"]), pd.Timestamp(bar["datetime"])
    return None


def _simulate_ticket(
    row: pd.Series, bars: pd.DataFrame, daily: pd.DataFrame, mode: str, hard_stop_pct: float
) -> dict[str, Any] | None:
    accepted = _first_acceptance(row, bars, mode)
    if accepted is None:
        return None
    entry, entry_ts = accepted
    entry_day = entry_ts.normalize()
    stop, take_profit = entry * (1.0 - hard_stop_pct), entry * 1.10
    remaining, realized, took_profit = 1.0, 0.0, False
    reason, exit_day = "hold20", entry_day

    def apply_bar(low: float, high: float, close: float, date: pd.Timestamp, prior_low: float | None) -> bool:
        nonlocal remaining, realized, took_profit, reason, exit_day
        if low <= stop:
            realized += remaining * (stop / entry - 1.0)
            remaining = 0.0
            reason, exit_day = "hard_stop", date
            return True
        if not took_profit and high >= take_profit:
            realized += 0.5 * (take_profit / entry - 1.0)
            remaining, took_profit = 0.5, True
        if took_profit and prior_low is not None and close < prior_low:
            realized += remaining * (close / entry - 1.0)
            remaining = 0.0
            reason, exit_day = "runner_previous_low_break", date
            return True
        return False

    same_day = bars[(bars["date"].eq(entry_day)) & (bars["datetime"] > entry_ts)].reset_index(drop=True)
    for idx, bar in same_day.iterrows():
        prior_low = _num(same_day.iloc[idx - 1]["low"]) if took_profit and idx else None
        if apply_bar(_num(bar["low"]), _num(bar["high"]), _num(bar["close"]), entry_day, prior_low):
            break

    future = daily[(daily["code_raw"].astype(str).eq(str(row["code_raw"]))) & (daily["trade_date"] > entry_day)].head(20).reset_index(drop=True)
    for idx, bar in future.iterrows() if remaining else []:
        date = pd.Timestamp(bar["trade_date"]).normalize()
        prior_low = _num(future.iloc[idx - 1]["low"]) if took_profit and idx else None
        if apply_bar(_num(bar["low"]), _num(bar["high"]), _num(bar["close"]), date, prior_low):
            break
        if idx == len(future) - 1:
            realized += remaining * (_num(bar["close"]) / entry - 1.0)
            remaining, exit_day = 0.0, date
    if remaining:
        return None
    result = row.to_dict()
    result.update({
        "entry_datetime": entry_ts.strftime("%Y-%m-%d %H:%M:%S"), "entry_price": entry,
        "exit_date": exit_day.strftime("%Y-%m-%d"), "gross_ret": realized, "net_ret": realized - FEE,
        "exit_reason": reason, "hard_stop": stop, "take_profit_1": take_profit,
        "took_profit": took_profit, "entry_mode": f"first_completed_30m_{mode}_acceptance",
    })
    return result


def _two_slot(tickets: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    if tickets.empty:
        return tickets.copy(), tickets.copy()
    work = tickets.copy()
    # Slots compete when a 30-minute acceptance actually occurs, not at the
    # start of the signal day.  Sorting only by daily rank would let a later
    # intraday confirmation displace an earlier one (a look-ahead bias).
    work["entry_ts"] = pd.to_datetime(work["entry_datetime"], errors="coerce")
    work["entry_date_ts"] = work["entry_ts"].dt.normalize()
    work["exit_date_ts"] = pd.to_datetime(work["exit_date"]).dt.normalize()
    work = work.dropna(subset=["entry_ts", "entry_date_ts", "exit_date_ts"])
    work = work.sort_values(["entry_ts", "daily_rank", "rank_key"], ascending=[True, True, False])
    active: list[dict] = []
    entered, skipped = [], []
    for ticket in work.to_dict("records"):
        active = [item for item in active if item["exit_date_ts"] >= ticket["entry_date_ts"]]
        if len(active) >= 2:
            ticket["slot_decision"] = "skipped_slots_occupied"
            skipped.append(ticket)
        else:
            ticket["slot_decision"] = "entered_slot"
            ticket["slot_id"] = len(active) + 1
            entered.append(ticket)
            active.append(ticket)
    return pd.DataFrame(entered), pd.DataFrame(skipped)


def _summary(trades: pd.DataFrame) -> dict[str, Any]:
    if trades.empty or "net_ret" not in trades.columns:
        return {
            "trades": 0,
            "win_rate": None,
            "avg_ret": None,
            "avg_win": None,
            "avg_loss": None,
            "payoff_ratio": None,
            "worst_trade": None,
        }
    ret = pd.to_numeric(trades["net_ret"], errors="coerce").dropna()
    wins, losses = ret[ret > 0], ret[ret <= 0]
    return {
        "trades": int(len(ret)), "win_rate": float((ret > 0).mean()) if len(ret) else None,
        "avg_ret": float(ret.mean()) if len(ret) else None, "avg_win": float(wins.mean()) if len(wins) else None,
        "avg_loss": float(losses.mean()) if len(losses) else None,
        "payoff_ratio": float(wins.mean() / abs(losses.mean())) if len(wins) and len(losses) else None,
        "worst_trade": float(ret.min()) if len(ret) else None,
    }


def _realised_proxy_curve(trades: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, float | None]]:
    """Exit-date proxy only; never present this as intraday mark-to-market."""
    if trades.empty:
        return pd.DataFrame(), {"realised_proxy_return": None, "realised_proxy_max_drawdown": None}
    curve = trades.copy()
    curve["exit_date"] = pd.to_datetime(curve["exit_date"], errors="coerce").dt.normalize()
    curve = curve.groupby("exit_date", as_index=False)["net_ret"].sum().sort_values("exit_date")
    curve["portfolio_ret"] = curve["net_ret"] * 0.5
    curve["equity"] = (1.0 + curve["portfolio_ret"]).cumprod()
    curve["drawdown"] = curve["equity"] / curve["equity"].cummax() - 1.0
    return curve, {
        "realised_proxy_return": float(curve["equity"].iloc[-1] - 1.0),
        "realised_proxy_max_drawdown": float(curve["drawdown"].min()),
    }


def _two_slot_funded_curve(trades: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, float | None]]:
    """Replay the selected two slots with capital reserved from entry to exit.

    Open positions remain at principal until their recorded exit.  This avoids
    inventing intraday marks while still giving the contract a capital-aware
    equity curve rather than an exit-date-only return aggregation.
    """
    if trades.empty:
        return pd.DataFrame(), {"funded_return": None, "funded_max_drawdown": None}
    work = trades.copy()
    work["entry_date_ts"] = pd.to_datetime(work["entry_datetime"], errors="coerce").dt.normalize()
    work["exit_date_ts"] = pd.to_datetime(work["exit_date"], errors="coerce").dt.normalize()
    work = work.dropna(subset=["entry_date_ts", "exit_date_ts", "net_ret"]).sort_values(["entry_datetime", "daily_rank"])
    if work.empty:
        return pd.DataFrame(), {"funded_return": None, "funded_max_drawdown": None}
    calendar = base._trade_calendar(work["entry_date_ts"].min(), work["exit_date_ts"].max())
    entries = {day: g.copy() for day, g in work.groupby("entry_date_ts")}
    cash = 1.0
    open_positions: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for day in calendar:
        realized = 0.0
        remaining: list[dict[str, Any]] = []
        for position in open_positions:
            if position["exit_date_ts"] <= day:
                exit_value = float(position["stake"]) * (1.0 + float(position["net_ret"]))
                cash += exit_value
                realized += exit_value - float(position["stake"])
            else:
                remaining.append(position)
        open_positions = remaining
        opened = 0
        for _, ticket in entries.get(day, pd.DataFrame()).iterrows():
            if len(open_positions) >= 2:
                continue
            equity_before = cash + sum(float(position["stake"]) for position in open_positions)
            stake = equity_before * 0.50
            if cash < stake:
                continue
            position = ticket.to_dict()
            position["stake"] = stake
            cash -= stake
            open_positions.append(position)
            opened += 1
        reserved = sum(float(position["stake"]) for position in open_positions)
        rows.append({"date": day, "cash": cash, "reserved_principal": reserved, "equity": cash + reserved, "open_positions": len(open_positions), "opened": opened, "realized_pnl": realized})
    curve = pd.DataFrame(rows)
    curve["peak"] = curve["equity"].cummax()
    curve["drawdown"] = curve["equity"] / curve["peak"] - 1.0
    curve["ret_from_start"] = curve["equity"] - 1.0
    return curve, {"funded_return": float(curve["equity"].iloc[-1] - 1.0), "funded_max_drawdown": float(curve["drawdown"].min())}


def main() -> int:
    parser = argparse.ArgumentParser(description="Recovered-mainwave 30m/two-slot research replay; never creates orders.")
    parser.add_argument("--start-date", default="2020-01-01")
    parser.add_argument("--end-date", default="2026-06-30")
    parser.add_argument("--minute-chunk-days", type=int, default=80)
    parser.add_argument("--acceptance-mode", choices=["ma20", "breakout"], default="ma20")
    parser.add_argument("--daily-candidate-limit", type=int, default=10,
                        help="Signal-only daily shortlist before 30m confirmation; positions remain capped at two.")
    parser.add_argument("--min-wave-score", type=float, default=88.0,
                        help="Signal-date mainwave score floor; does not use scheduler or outcome labels.")
    parser.add_argument("--min-sector-signal-count", type=int, default=1,
                        help="Same-day, same-industry mainwave skeleton count; 2 means at least one confirming peer.")
    parser.add_argument("--hard-stop-pct", type=float, default=0.10,
                        help="Per-ticket hard-stop fraction; research-only OHLC proxy.")
    args = parser.parse_args()
    if args.daily_candidate_limit < 1 or args.min_sector_signal_count < 1 or not 0 < args.hard_stop_pct < 0.20:
        raise ValueError("invalid candidate limit, sector count, or hard-stop-pct")
    run_name = f"{args.start_date.replace('-', '')}_{args.end_date.replace('-', '')}_{args.acceptance_mode}_score{args.min_wave_score:g}_sector{args.min_sector_signal_count}_stop{args.hard_stop_pct:.0%}_top{args.daily_candidate_limit}"
    out = OUT_ROOT / run_name
    out.mkdir(parents=True, exist_ok=True)
    candidates, daily = _daily_candidates(
        args.start_date, args.end_date, args.daily_candidate_limit, args.min_wave_score, args.min_sector_signal_count
    )
    bars = _load_m30_for_candidate_days(candidates, chunk_days=max(5, int(args.minute_chunk_days)))
    tickets = pd.DataFrame([
        x for _, row in candidates.iterrows()
        if (x := _simulate_ticket(row, bars.get(str(row["code_raw"]), pd.DataFrame()), daily, args.acceptance_mode, args.hard_stop_pct)) is not None
    ])
    if not tickets.empty:
        tickets["strategy_id"] = "g3_institutional_mainwave_score88_v1"
        tickets["contract"] = "g3_institutional_mainwave_score88_v1"
        tickets["router_eligible"] = True
        tickets["m30_confirmed"] = True
        tickets["source_quality"] = "qmt_canonical_research_ohlc_proxy"
        tickets["account_risk_action"] = "not_modelled_research_only"
    entered, skipped = _two_slot(tickets)
    curve, curve_summary = _realised_proxy_curve(entered)
    funded_curve, funded_summary = _two_slot_funded_curve(entered)
    for frame, name in [(candidates, "daily_shortlist_candidates.csv"), (tickets, "m30_confirmed_tickets.csv"), (entered, "two_slot_closed_trades.csv"), (skipped, "slot_skipped_tickets.csv"), (curve, "two_slot_realised_proxy_curve.csv"), (funded_curve, "two_slot_funded_equity_curve.csv")]:
        frame.to_csv(out / name, index=False, encoding="utf-8-sig")
    payload = {
        "status": "completed", "index_code": INDEX_CODE,
        "contract": f"wave_style_score>={args.min_wave_score:g} + index_mom60<=5% + same-day sector signals>={args.min_sector_signal_count} -> daily top{args.daily_candidate_limit} shortlist -> first completed 30m {args.acceptance_mode} acceptance -> -{args.hard_stop_pct:.0%} stop, +10% half, runner previous-low exit -> two 50% slots",
        "daily_shortlist_candidates": int(len(candidates)), "m30_confirmed_tickets": int(len(tickets)), "slot_skipped": int(len(skipped)),
        "two_slot": _summary(entered), **curve_summary, **funded_summary,
        "limitations": "Research-only OHLC proxy. Stops are conservative stop-first within a bar; no tick slippage, price-limit or actual order-fill model. Funded curve reserves entry capital until exit but does not mark open positions intraday."
    }
    (out / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 恢复主升骨架：30分钟承接与二槽合同", "", "- 研究用途，不连接候选池或下单。", f"- 日线候选：`{len(candidates)}`；30分钟确认：`{len(tickets)}`；二槽成交：`{len(entered)}`。", "", "| 指标 | 值 |", "|---|---:|"]
    for key, value in _summary(entered).items():
        text = f"{value:.2%}" if isinstance(value, float) and ("ret" in key or key in {"win_rate", "payoff_ratio"}) else str(value)
        lines.append(f"| {key} | {text} |")
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

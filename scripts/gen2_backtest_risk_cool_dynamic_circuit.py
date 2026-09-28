from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen2_backtest_open_v1_portfolio import (  # noqa: E402
    _json_default,
    _load_benchmark,
    _load_signals,
    _load_trade_dates,
    _safe_price,
    _sort_signals,
)
from scripts.gen2_backtest_open_v1_intraday_risk import Lot, _sell_trade  # noqa: E402
from scripts.gen2_candidate_outcome_supervision import _prepare_source  # noqa: E402
from scripts.gen2_compare_prev_low_exit_fills import (  # noqa: E402
    FillProfile,
    _load_minute_bars,
    _process_lot_exits,
)
from scripts.gen2_runtime_dates import add_end_date_argument, resolve_end_date  # noqa: E402
from scripts.gen2_sweep_open_v1_semantic_exits import _load_daily_ohlc  # noqa: E402


DEFAULT_CANDIDATES = ROOT / "reports" / "gen2_candidate_outcome_supervision" / "labeled_candidates.parquet"
DEFAULT_OUTPUT_DIR = ROOT / "reports" / "gen2_risk_cool_dynamic_circuit"
DEFAULT_SHARE_CAP_CACHE = ROOT / "data" / "runtime" / "tdx_share_cap_history.parquet"


def _pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def _base_mask(d: pd.DataFrame) -> pd.Series:
    return (
        pd.to_numeric(d["mom20"], errors="coerce").le(0.30)
        & pd.to_numeric(d["mom5"], errors="coerce").le(0.14)
        & pd.to_numeric(d["vol_ratio"], errors="coerce").le(1.90)
        & pd.to_numeric(d["rt_return_from_d1_close"], errors="coerce").between(0.00, 0.12)
        & pd.to_numeric(d["rt_30m_amount_ratio"], errors="coerce").between(1.5, 7.0)
    )


def _load_filter_daily(codes: list[str], start_date: str, end_date: str) -> pd.DataFrame:
    if not codes:
        return pd.DataFrame()
    from utils.market_warehouse import clickhouse_client

    quoted = ", ".join(f"'{code}'" for code in sorted(set(codes)))
    start = (pd.Timestamp(start_date) - pd.Timedelta(days=430)).strftime("%Y-%m-%d")
    ch = clickhouse_client()
    df = ch.query_df(
        f"""
        SELECT code, trade_date, open, high, low, close, volume, amount
        FROM kline_daily
        WHERE code IN ({quoted})
          AND trade_date BETWEEN '{start}' AND '{end_date}'
        ORDER BY code, trade_date
        """
    )
    if df.empty:
        return df
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["code", "trade_date", "close"]).reset_index(drop=True)


def _build_signal_source(
    candidates_path: Path,
    output_dir: Path,
    start_date: str,
    end_date: str,
    preference_filter: str,
    share_cap_cache: Path,
    require_cap_data: bool,
) -> Path:
    d = pd.read_parquet(candidates_path)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    selected = d[_base_mask(d).fillna(False)].copy()
    filter_summary: dict[str, Any] = {
        "preference_filter": preference_filter,
        "base_signals": int(len(selected)),
        "share_cap_cache": str(share_cap_cache),
        "require_cap_data": bool(require_cap_data),
    }
    if preference_filter == "user_v2" and not selected.empty:
        from scripts.gen2_preference_filters import apply_user_v2_filters, load_share_cap_cache, summarize_filter_result

        codes = selected["code"].dropna().astype(str).unique().tolist()
        daily = _load_filter_daily(codes, start_date, end_date)
        share_cap = load_share_cap_cache(share_cap_cache)
        audited = apply_user_v2_filters(
            selected,
            daily=daily,
            share_cap=share_cap,
            require_cap_data=require_cap_data,
        )
        filter_summary.update(summarize_filter_result(audited))
        audit_cols = [
            "entry_date",
            "code",
            "name",
            "v4_rank",
            "v4_score",
            "rank_change_status",
            "rank_change",
            "score_change",
            "entry_price",
            "float_market_cap_yi",
            "cap_bucket",
            "cap_pressure_lookback_days",
            "cap_pressure_days",
            "cap_big_pressure_days",
            "cap_pressure_amount_share",
            "overhead_pressure_days",
            "overhead_big_pressure_days",
            "overhead_pressure_amount_share",
            "prior_max_high_ratio",
            "runup_from_60d_low",
            "recent60_return",
            "cap_pressure_reject",
            "downtrend_rebound_reject",
            "downtrend_rebound_reason",
            "downtrend_rebound_into_pressure",
            "share_cap_missing",
            "user_v2_reject_reason",
            "user_v2_pass",
        ]
        audit_path = output_dir / "user_v2_filter_audit.csv"
        output_dir.mkdir(parents=True, exist_ok=True)
        audited[[col for col in audit_cols if col in audited.columns]].to_csv(audit_path, index=False, encoding="utf-8-sig")
        selected = audited[audited["user_v2_pass"].fillna(False)].copy()
    elif preference_filter not in {"off", "user_v2"}:
        raise ValueError(f"Unknown preference_filter: {preference_filter}")
    prepared = _prepare_source(selected)
    output_dir.mkdir(parents=True, exist_ok=True)
    source = output_dir / "risk_cool_base.parquet"
    prepared.to_parquet(source, index=False)
    prepared.to_csv(source.with_suffix(".csv"), index=False, encoding="utf-8-sig")
    filter_summary["signals_after_preference_filter"] = int(len(prepared))
    (output_dir / "filter_summary.json").write_text(
        json.dumps(filter_summary, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    return source


def _policy_params(policy: str) -> tuple[int, str, int]:
    table = {
        "base_full": (0, "none", 1),
        "stop_cd3_skip": (3, "skip", 1),
        "stop_cd3_half": (3, "half", 1),
        "stop_cd5_skip": (5, "skip", 1),
        "stop_cd5_half": (5, "half", 1),
        "two_stop_cd3_skip": (3, "skip", 2),
        "two_stop_cd5_skip": (5, "skip", 2),
        "two_stop_cd3_half": (3, "half", 2),
    }
    if policy not in table:
        raise ValueError(f"Unknown policy: {policy}")
    return table[policy]


def _portfolio_guard_params(policy: str) -> tuple[float | None, str]:
    table = {
        "dd6_half_stop_cd3_skip": (0.06, "half"),
        "dd8_half_stop_cd3_skip": (0.08, "half"),
        "dd10_half_stop_cd3_skip": (0.10, "half"),
        "dd6_skip_stop_cd3_skip": (0.06, "skip"),
        "dd8_skip_stop_cd3_skip": (0.08, "skip"),
    }
    for prefix, params in {
        "dd6_half_": (0.06, "half"),
        "dd8_half_": (0.08, "half"),
        "dd10_half_": (0.10, "half"),
        "dd6_skip_": (0.06, "skip"),
        "dd8_skip_": (0.08, "skip"),
    }.items():
        if policy.startswith(prefix):
            return params
    return table.get(policy, (None, "none"))


def _portfolio_guard_weight(policy: str, drawdown: float) -> tuple[bool, float, str]:
    if policy.startswith("dd_tier_70_50_25"):
        if drawdown <= -0.12:
            return True, 0.25, "tier_25"
        if drawdown <= -0.08:
            return True, 0.50, "tier_50"
        if drawdown <= -0.06:
            return True, 0.70, "tier_70"
        return False, 1.0, "none"
    if policy.startswith("dd_tier_80_50_25"):
        if drawdown <= -0.12:
            return True, 0.25, "tier_25"
        if drawdown <= -0.08:
            return True, 0.50, "tier_50"
        if drawdown <= -0.06:
            return True, 0.80, "tier_80"
        return False, 1.0, "none"

    guard_drawdown, guard_action = _portfolio_guard_params(policy)
    if guard_drawdown is None or drawdown > -float(guard_drawdown):
        return False, 1.0, "none"
    if guard_action == "skip":
        return True, 0.0, "skip"
    if guard_action == "half":
        return True, 0.5, "half"
    return True, 1.0, str(guard_action)


def _rolling_stop_guard_params(policy: str) -> tuple[int, int, int, str]:
    if "roll20_2stop_cd5_half" in policy:
        return 20, 2, 5, "half"
    if "roll20_2stop_cd5_skip" in policy:
        return 20, 2, 5, "skip"
    return 0, 0, 0, "none"


def _segment_rows(curve_df: pd.DataFrame) -> list[dict[str, Any]]:
    d = curve_df.copy()
    d["date"] = pd.to_datetime(d["date"], errors="coerce")
    windows = [
        ("2024H2-2025Q1", "2024-07-09", "2025-03-31"),
        ("2025Q2-Q4", "2025-04-01", "2025-12-31"),
        ("2026YTD", "2026-01-01", "2026-05-21"),
    ]
    rows: list[dict[str, Any]] = []
    for name, start, end in windows:
        g = d[(d["date"] >= pd.Timestamp(start)) & (d["date"] <= pd.Timestamp(end))].copy()
        if g.empty:
            continue
        first = float(g.iloc[0]["strategy_equity"])
        last = float(g.iloc[-1]["strategy_equity"])
        rows.append({"segment": name, "return": last / first - 1.0 if first else None, "max_drawdown": float(g["drawdown"].min())})
    return rows


def _run_dynamic(
    signal_source: Path,
    output_dir: Path,
    policy: str,
    start_date: str,
    end_date: str,
    sort_mode: str = "trigger_time",
    stop_loss_pct: float = 0.05,
    take_profit_pct: float = 0.10,
    take_profit_sell_ratio: float = 0.50,
    max_hold_days: int = 10,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    base_policy = "stop_cd3_skip" if policy.startswith("dd") else policy
    cooldown_days, action, trigger_stop_count = _policy_params(base_policy)
    guard_drawdown, guard_action = _portfolio_guard_params(policy)
    rolling_window, rolling_trigger, rolling_cooldown_days, rolling_action = _rolling_stop_guard_params(policy)
    profile = FillProfile("gap_confirm_30m_close__intraday_30m_close", gap_open_mode="confirm_30m", intraday_mode="bar_close")
    signals = _load_signals(signal_source, start_date, end_date)
    signals["confirm_datetime"] = pd.to_datetime(signals["confirm_datetime"], errors="coerce")
    signals = signals.dropna(subset=["confirm_datetime"]).copy()
    trade_dates = _load_trade_dates(start_date, end_date)
    if not trade_dates:
        raise RuntimeError("No trade dates available for backtest.")
    date_idx = {date: i for i, date in enumerate(trade_dates)}
    signals = signals[signals["entry_date"].isin(date_idx)].copy()
    signals = _sort_signals(signals, sort_mode).reset_index(drop=True)
    signals_by_date = {date: g.copy() for date, g in signals.groupby("entry_date", sort=True)}

    codes = signals["code"].dropna().astype(str).unique().tolist()
    daily = _load_daily_ohlc(codes, start_date, end_date)
    price_map = {(row.code, row.trade_date): float(row.close) for row in daily.itertuples(index=False)}
    prev_low_map = {
        (row.code, row.trade_date): float(row.prev_low)
        for row in daily.itertuples(index=False)
        if row.prev_low is not None and np.isfinite(row.prev_low)
    }
    bars30 = _load_minute_bars(codes, start_date, end_date, 30)
    bars30_by_code_date = {
        (code, date): g.sort_values("datetime").copy()
        for (code, date), g in bars30.groupby(["code", "bar_date"], sort=False)
    }

    cash = 150000.0
    lots: list[Lot] = []
    trades: list[dict[str, Any]] = []
    curve: list[dict[str, Any]] = []
    decision_ledger: list[dict[str, Any]] = []
    cooldown_until_idx = -1
    consecutive_stop_events = 0
    portfolio_peak_equity = 150000.0
    rolling_stop_indices: list[int] = []
    rolling_stop_cooldown_until_idx = -1

    for date in trade_dates:
        idx = date_idx[date]
        stop_events_today = 0
        next_lots: list[Lot] = []
        for lot in lots:
            before_trades = len(trades)
            day_bars30 = bars30_by_code_date.get((lot.code, date), pd.DataFrame())
            if not day_bars30.empty:
                day_bars30 = day_bars30[day_bars30["datetime"] > lot.buy_datetime].copy()
            lot_after, cash = _process_lot_exits(
                lot=lot,
                bars30=day_bars30,
                semantic_bars=day_bars30,
                cash=cash,
                trades=trades,
                prev_low=prev_low_map.get((lot.code, date)),
                profile=profile,
                gap_confirm_bars=day_bars30,
                sell_slippage_bps=5.0,
                commission_bps=2.5,
                stamp_tax_bps=5.0,
                stop_loss_pct=stop_loss_pct,
                take_profit_pct=take_profit_pct,
                take_profit_sell_ratio=take_profit_sell_ratio,
            )
            new_trades = trades[before_trades:]
            stop_events_today += sum(1 for t in new_trades if str(t.get("exit_reason")) == "stop_loss_30m")
            if lot_after is None:
                continue
            if date >= lot_after.sell_date:
                close_price = _safe_price(price_map, lot_after.code, date)
                if close_price is not None:
                    trade = _sell_trade(
                        lot=lot_after,
                        sell_datetime=pd.Timestamp(f"{date} 15:00:00"),
                        raw_price=close_price,
                        ratio=1.0,
                        reason="time_exit",
                        sell_slippage_bps=5.0,
                        commission_bps=2.5,
                        stamp_tax_bps=5.0,
                    )
                    cash += float(trade["proceeds"])
                    trades.append({k: v for k, v in trade.items() if k != "proceeds"})
                else:
                    next_lots.append(lot_after)
            else:
                next_lots.append(lot_after)
        lots = next_lots

        if stop_events_today:
            consecutive_stop_events += int(stop_events_today)
            if cooldown_days > 0 and consecutive_stop_events >= trigger_stop_count:
                cooldown_until_idx = max(cooldown_until_idx, idx + cooldown_days)
                consecutive_stop_events = 0
        else:
            consecutive_stop_events = 0

        cooldown_active = idx <= cooldown_until_idx
        if rolling_window > 0:
            if stop_events_today:
                rolling_stop_indices.extend([idx] * int(stop_events_today))
            rolling_stop_indices = [event_idx for event_idx in rolling_stop_indices if event_idx >= idx - rolling_window + 1]
            if stop_events_today and len(rolling_stop_indices) >= rolling_trigger:
                rolling_stop_cooldown_until_idx = max(rolling_stop_cooldown_until_idx, idx + rolling_cooldown_days)
        rolling_stop_guard_active = bool(rolling_window > 0 and idx <= rolling_stop_cooldown_until_idx)

        capital_weight = 1.0
        buy_allowed = True
        if cooldown_active:
            if action == "skip":
                buy_allowed = False
                capital_weight = 0.0
            elif action == "half":
                capital_weight = 0.5

        pre_buy_market_value = 0.0
        for lot in lots:
            close = _safe_price(price_map, lot.code, date) or lot.buy_price
            pre_buy_market_value += lot.shares * close
        pre_buy_equity = cash + pre_buy_market_value
        if pre_buy_equity > portfolio_peak_equity:
            portfolio_peak_equity = pre_buy_equity
        portfolio_drawdown = pre_buy_equity / portfolio_peak_equity - 1.0 if portfolio_peak_equity > 0 else 0.0
        guard_active, guard_weight, guard_weight_reason = _portfolio_guard_weight(policy, portfolio_drawdown)
        if guard_active:
            if guard_weight <= 0:
                buy_allowed = False
                capital_weight = 0.0
            else:
                capital_weight = min(capital_weight, float(guard_weight))

        if rolling_stop_guard_active:
            if rolling_action == "skip":
                buy_allowed = False
                capital_weight = 0.0
            elif rolling_action == "half":
                capital_weight = min(capital_weight, 0.5)

        bought = 0
        today = signals_by_date.get(date)
        if buy_allowed and today is not None and not today.empty:
            held_codes = {lot.code for lot in lots}
            slots = max(0, 2 - len(lots))
            buy_count = min(slots, 1)
            if buy_count > 0 and cash > 0:
                candidates = today[~today["code"].isin(held_codes)].head(buy_count)
                market_value_for_size = 0.0
                for lot in lots:
                    close = _safe_price(price_map, lot.code, date) or lot.buy_price
                    market_value_for_size += lot.shares * close
                sizing_equity = cash + market_value_for_size
                target_capital = min(sizing_equity / 2.0, sizing_equity * 0.50) * capital_weight
                for row in candidates.itertuples(index=False):
                    signal_weight_raw = getattr(row, "alpha191_position_weight", 1.0)
                    try:
                        signal_weight = float(signal_weight_raw)
                    except (TypeError, ValueError):
                        signal_weight = 1.0
                    if not np.isfinite(signal_weight) or signal_weight <= 0:
                        signal_weight = 1.0
                    capital_each = min(cash, target_capital * signal_weight)
                    if capital_each <= 0:
                        break
                    buy_price = float(getattr(row, "entry_price")) * 1.0005
                    if buy_price <= 0 or not np.isfinite(buy_price):
                        continue
                    entry_idx = date_idx[date]
                    sell_idx = min(entry_idx + int(max_hold_days), len(trade_dates) - 1)
                    buy_fee = capital_each * 2.5 / 10000.0
                    shares = (capital_each - buy_fee) / buy_price
                    cash -= capital_each
                    lots.append(
                        Lot(
                            code=str(getattr(row, "code")),
                            name=str(getattr(row, "name")),
                            buy_date=date,
                            buy_datetime=pd.Timestamp(getattr(row, "confirm_datetime")),
                            sell_date=trade_dates[sell_idx],
                            buy_price=buy_price,
                            shares=shares,
                            capital=capital_each,
                            v4_rank=int(getattr(row, "v4_rank")),
                            v4_score=float(getattr(row, "v4_score")),
                            peak_price=buy_price,
                        )
                    )
                    bought += 1

        market_value = 0.0
        for lot in lots:
            close = _safe_price(price_map, lot.code, date) or lot.buy_price
            market_value += lot.shares * close
        equity = cash + market_value
        curve.append({"date": date, "cash": cash, "market_value": market_value, "equity": equity, "holding_count": len(lots)})
        decision_ledger.append(
            {
                "date": date,
                "cooldown_active": bool(cooldown_active),
                "cooldown_until_idx": int(cooldown_until_idx),
                "stop_events_today": int(stop_events_today),
                "buy_allowed": bool(buy_allowed),
                "capital_weight": float(capital_weight),
                "portfolio_drawdown": float(portfolio_drawdown),
                "guard_active": bool(guard_active),
                "guard_weight_reason": str(guard_weight_reason),
                "rolling_stop_guard_active": bool(rolling_stop_guard_active),
                "rolling_stop_count": int(len(rolling_stop_indices)),
                "rolling_stop_cooldown_until_idx": int(rolling_stop_cooldown_until_idx),
                "bought": int(bought),
            }
        )

    curve_df = pd.DataFrame(curve)
    trades_df = pd.DataFrame(trades)
    ledger_df = pd.DataFrame(decision_ledger)
    if not curve_df.empty:
        curve_df["strategy_equity"] = curve_df["equity"] / 150000.0
        curve_df["peak"] = curve_df["strategy_equity"].cummax()
        curve_df["drawdown"] = curve_df["strategy_equity"] / curve_df["peak"] - 1.0
    benchmark = _load_benchmark(start_date, end_date, "000852.SH")
    if not benchmark.empty:
        curve_df = curve_df.merge(benchmark, on="date", how="left")

    total_return = float(curve_df["strategy_equity"].iloc[-1] - 1.0) if not curve_df.empty else 0.0
    benchmark_return = (
        float(curve_df["benchmark_equity"].dropna().iloc[-1] - 1.0)
        if "benchmark_equity" in curve_df.columns and not curve_df["benchmark_equity"].dropna().empty
        else None
    )
    summary = {
        "schema_version": 1,
        "policy": policy,
        "base_policy": base_policy,
        "cooldown_days": int(cooldown_days),
        "cooldown_action": action,
        "trigger_stop_count": int(trigger_stop_count),
        "portfolio_guard_drawdown": guard_drawdown,
        "portfolio_guard_action": guard_action,
        "rolling_stop_window": int(rolling_window),
        "rolling_stop_trigger": int(rolling_trigger),
        "rolling_stop_cooldown_days": int(rolling_cooldown_days),
        "rolling_stop_action": rolling_action,
        "profile": profile.__dict__,
        "stop_loss_pct": float(stop_loss_pct),
        "take_profit_pct": float(take_profit_pct),
        "take_profit_sell_ratio": float(take_profit_sell_ratio),
        "max_hold_days": int(max_hold_days),
        "sort_mode": sort_mode,
        "start_date": start_date,
        "end_date": end_date,
        "signal_count": int(len(signals)),
        "trade_count": int(len(trades_df)),
        "cooldown_active_days": int(ledger_df["cooldown_active"].sum()) if not ledger_df.empty else 0,
        "portfolio_guard_active_days": int(ledger_df["guard_active"].sum()) if "guard_active" in ledger_df.columns else 0,
        "rolling_stop_guard_active_days": int(ledger_df["rolling_stop_guard_active"].sum()) if "rolling_stop_guard_active" in ledger_df.columns else 0,
        "skipped_buy_days": int((~ledger_df["buy_allowed"]).sum()) if not ledger_df.empty else 0,
        "half_weight_days": int((ledger_df["capital_weight"] == 0.5).sum()) if not ledger_df.empty else 0,
        "reduced_weight_days": int((ledger_df["capital_weight"].between(0.01, 0.999999)).sum()) if "capital_weight" in ledger_df.columns else 0,
        "final_equity": float(curve_df["equity"].iloc[-1]) if not curve_df.empty else 150000.0,
        "total_return": total_return,
        "benchmark_return": benchmark_return,
        "excess_return": None if benchmark_return is None else total_return - benchmark_return,
        "max_drawdown": float(curve_df["drawdown"].min()) if "drawdown" in curve_df.columns else 0.0,
        "win_rate": float((trades_df["return"] > 0).mean()) if not trades_df.empty else None,
        "avg_trade_return": float(trades_df["return"].mean()) if not trades_df.empty else None,
        "exit_reason_counts": trades_df["exit_reason"].fillna("").astype(str).value_counts().to_dict() if not trades_df.empty else {},
    }
    signals.to_csv(output_dir / "signals.csv", index=False, encoding="utf-8-sig")
    trades_df.to_csv(output_dir / "trades.csv", index=False, encoding="utf-8-sig")
    curve_df.to_csv(output_dir / "equity_curve.csv", index=False, encoding="utf-8-sig")
    ledger_df.to_csv(output_dir / "decision_ledger.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(_segment_rows(curve_df)).to_csv(output_dir / "segment_summary.csv", index=False, encoding="utf-8-sig")
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return summary


def _write_report(output_dir: Path, rows: list[dict[str, Any]]) -> None:
    lines = [
        "# G2 risk_cool Dynamic Circuit Backtest",
        "",
        "Cooldown state is updated from actual trades produced by each policy during the simulation.",
        "",
        "| policy | cd | action | active_days | skipped_days | half_days | trades | total | excess | max_dd | win | avg_trade | exits |",
        "| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in rows:
        lines.append(
            f"| {row['policy']} | {row['cooldown_days']} | {row['cooldown_action']} | {row['cooldown_active_days']} | "
            f"{row['skipped_buy_days']} | {row['half_weight_days']} | {row['trade_count']} | {_pct(row['total_return'])} | "
            f"{_pct(row['excess_return'])} | {_pct(row['max_drawdown'])} | {_pct(row['win_rate'])} | "
            f"{_pct(row['avg_trade_return'])} | {json.dumps(row.get('exit_reason_counts', {}), ensure_ascii=False, sort_keys=True)} |"
        )
    (output_dir / "dynamic_circuit_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    source = _build_signal_source(
        candidates_path=Path(args.candidates),
        output_dir=output_dir / "sources",
        start_date=str(args.start_date),
        end_date=str(args.end_date),
        preference_filter=str(args.preference_filter),
        share_cap_cache=Path(args.share_cap_cache),
        require_cap_data=bool(args.require_cap_data),
    )
    policies = [x.strip() for x in str(args.policies).split(",") if x.strip()]
    rows = []
    for policy in policies:
        rows.append(
            _run_dynamic(
                signal_source=source,
                output_dir=output_dir / "backtests" / policy,
                policy=policy,
                start_date=str(args.start_date),
                end_date=str(args.end_date),
                sort_mode="trigger_time",
            )
        )
    df = pd.DataFrame(rows).sort_values(["max_drawdown", "total_return"], ascending=[False, False])
    df.to_csv(output_dir / "summary.csv", index=False, encoding="utf-8-sig")
    _write_report(output_dir, df.to_dict("records"))
    payload = {
        "schema_version": 1,
        "rows": df.where(pd.notna(df), None).to_dict("records"),
        "outputs": {"summary": "summary.csv", "report": "dynamic_circuit_report.md", "runs": "backtests/"},
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Dynamic circuit-breaker backtest for G2 risk_cool_v2.")
    parser.add_argument("--candidates", default=str(DEFAULT_CANDIDATES))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--start-date", default="2024-07-09")
    add_end_date_argument(parser)
    parser.add_argument("--policies", default="base_full,stop_cd3_skip,stop_cd3_half,stop_cd5_skip,stop_cd5_half,two_stop_cd3_skip,two_stop_cd5_skip,two_stop_cd3_half")
    parser.add_argument("--preference-filter", choices=["off", "user_v2"], default="off")
    parser.add_argument("--share-cap-cache", default=str(DEFAULT_SHARE_CAP_CACHE))
    parser.add_argument("--require-cap-data", action="store_true")
    args = parser.parse_args()
    args.end_date = resolve_end_date(args.end_date)
    print(json.dumps(run(args), ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts.gen2_backtest_open_v1_intraday_risk import Lot, _sell_trade  # noqa: E402
from scripts.gen2_backtest_open_v1_portfolio import (  # noqa: E402
    _json_default,
    _load_benchmark,
    _load_trade_dates,
    _pct,
    _safe_price,
)
from scripts.gen2_compare_prev_low_exit_fills import (  # noqa: E402
    FillProfile,
    _load_minute_bars,
    _process_lot_exits,
)
from scripts.gen2_runtime_dates import add_end_date_argument, resolve_end_date  # noqa: E402
from scripts.gen2_sweep_open_v1_semantic_exits import _load_daily_ohlc  # noqa: E402
from utils.market_warehouse import clickhouse_client  # noqa: E402

DEFAULT_EVENT_DATASET = REPO_ROOT / "reports" / "gen2_event_study_full" / "v4_event_dataset.parquet"
DEFAULT_STATE_DAILY = REPO_ROOT / "reports" / "gen2_open_state_research_full" / "g2_open_state_daily.csv"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "reports" / "gen2_v2_trend_continuation_15m"


@dataclass(frozen=True)
class CandidateProfile:
    name: str
    source: str
    max_rank: int
    min_score: float
    min_mom20: float
    min_mom10: float
    min_mom5: float
    max_mom5: float
    max_dist_ma20: float
    max_vol_ratio: float
    max_vol10: float
    require_state: Optional[str] = None


@dataclass(frozen=True)
class TriggerProfile:
    name: str
    trigger_type: str
    vol_mult: float
    min_time: str


def _candidate_profiles() -> list[CandidateProfile]:
    return [
        CandidateProfile(
            name="pool_rank100_core",
            source="in_score_pool",
            max_rank=100,
            min_score=0.0,
            min_mom20=0.05,
            min_mom10=0.03,
            min_mom5=-0.02,
            max_mom5=0.08,
            max_dist_ma20=0.18,
            max_vol_ratio=2.5,
            max_vol10=0.08,
        ),
        CandidateProfile(
            name="pool_rank100_loose",
            source="in_score_pool",
            max_rank=100,
            min_score=0.0,
            min_mom20=0.03,
            min_mom10=0.02,
            min_mom5=-0.03,
            max_mom5=0.10,
            max_dist_ma20=0.22,
            max_vol_ratio=3.0,
            max_vol10=0.10,
        ),
        CandidateProfile(
            name="entry_rank100_core",
            source="entry_pass",
            max_rank=100,
            min_score=0.0,
            min_mom20=0.05,
            min_mom10=0.03,
            min_mom5=-0.02,
            max_mom5=0.08,
            max_dist_ma20=0.18,
            max_vol_ratio=2.5,
            max_vol10=0.08,
        ),
        CandidateProfile(
            name="pool_rank50_core",
            source="in_score_pool",
            max_rank=50,
            min_score=0.0,
            min_mom20=0.05,
            min_mom10=0.03,
            min_mom5=-0.02,
            max_mom5=0.08,
            max_dist_ma20=0.18,
            max_vol_ratio=2.5,
            max_vol10=0.08,
        ),
    ]


def _trigger_profiles() -> list[TriggerProfile]:
    return [
        TriggerProfile("break_day_high_vol12", "break_day_high", 1.2, "10:00:00"),
        TriggerProfile("break_day_high_vol10", "break_day_high", 1.0, "10:00:00"),
        TriggerProfile("break_open_range_high_vol12", "break_open_range_high", 1.2, "10:30:00"),
        TriggerProfile("break_prev_bar_high_vol12", "break_prev_bar_high", 1.2, "10:00:00"),
    ]


def _load_states(path: Path) -> pd.DataFrame:
    states = pd.read_csv(path)
    states["trade_date"] = pd.to_datetime(states["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    return states[["trade_date", "g2_open_state"]].dropna(subset=["trade_date"]).copy()


def _load_daily_features(codes: list[str], start_date: str, end_date: str) -> pd.DataFrame:
    if not codes:
        return pd.DataFrame()
    quoted = ", ".join([f"'{code}'" for code in sorted(set(codes))])
    ch = clickhouse_client()
    df = ch.query_df(
        f"""
        SELECT code, trade_date, close
        FROM kline_daily
        WHERE code IN ({quoted})
          AND trade_date BETWEEN subtractDays(toDate('{start_date}'), 80) AND toDate('{end_date}')
        ORDER BY code, trade_date
        """
    )
    if df.empty:
        return df
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["code", "trade_date", "close"]).sort_values(["code", "trade_date"]).reset_index(drop=True)
    df["stock_ma20"] = df.groupby("code")["close"].transform(lambda s: s.rolling(20, min_periods=20).mean())
    df["stock_ma20_slope5"] = df.groupby("code")["stock_ma20"].transform(lambda s: s / s.shift(5) - 1.0)
    df["dist_ma20"] = df["close"] / df["stock_ma20"] - 1.0
    return df[["code", "trade_date", "stock_ma20", "stock_ma20_slope5", "dist_ma20"]]


def _prepare_events(
    event_dataset: Path,
    state_daily: Path,
    start_date: str,
    end_date: str,
    trade_dates: list[str],
) -> pd.DataFrame:
    raw = pd.read_parquet(event_dataset)
    raw["trade_date"] = pd.to_datetime(raw["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    raw = raw[(raw["trade_date"] >= "2010-01-01") & (raw["trade_date"] <= end_date)].copy()
    raw["v4_rank"] = pd.to_numeric(raw["v4_rank"], errors="coerce").fillna(9999).astype(int)
    raw = raw[raw["v4_rank"] <= 150].copy()
    for col in ["v4_score", "mom5", "mom10", "mom20", "vol_ratio", "vol10", "close"]:
        raw[col] = pd.to_numeric(raw[col], errors="coerce")
    raw["entry_pass"] = raw["entry_pass"].astype(bool)
    raw["in_score_pool"] = raw["in_score_pool"].astype(bool)

    states = _load_states(state_daily)
    raw = raw.merge(states, on="trade_date", how="left")
    features = _load_daily_features(raw["code"].dropna().astype(str).unique().tolist(), start_date, end_date)
    raw = raw.merge(features, on=["code", "trade_date"], how="left")

    next_date = {trade_dates[i]: trade_dates[i + 1] for i in range(len(trade_dates) - 1)}
    raw["entry_date"] = raw["trade_date"].map(next_date)
    raw = raw.dropna(subset=["entry_date"]).copy()
    raw = raw[(raw["entry_date"] >= start_date) & (raw["entry_date"] <= end_date)].copy()
    return raw.reset_index(drop=True)


def _select_candidates(events: pd.DataFrame, profile: CandidateProfile) -> pd.DataFrame:
    source_mask = events[profile.source].astype(bool)
    mask = (
        source_mask
        & (events["v4_rank"] <= profile.max_rank)
        & (events["v4_score"] >= profile.min_score)
        & (events["mom20"] >= profile.min_mom20)
        & (events["mom10"] >= profile.min_mom10)
        & (events["mom5"] >= profile.min_mom5)
        & (events["mom5"] <= profile.max_mom5)
        & (events["vol_ratio"] <= profile.max_vol_ratio)
        & (events["vol10"] <= profile.max_vol10)
        & (events["close"] >= events["stock_ma20"])
        & (events["stock_ma20_slope5"] > 0)
        & (events["dist_ma20"] <= profile.max_dist_ma20)
    )
    if profile.require_state:
        mask = mask & (events["g2_open_state"] == profile.require_state)
    d = events[mask].copy()
    d["candidate_profile"] = profile.name
    return d.reset_index(drop=True)


def _load_intraday_shanghai_gate(start_date: str, end_date: str, period: int, trade_dates: list[str]) -> pd.DataFrame:
    table = {15: "kline_minute_15", 30: "kline_minute_30"}[int(period)]
    ch = clickhouse_client()
    daily = ch.query_df(
        f"""
        SELECT trade_date, close
        FROM kline_daily
        WHERE code = '999999.SH'
          AND trade_date BETWEEN subtractDays(toDate('{start_date}'), 80) AND toDate('{end_date}')
        ORDER BY trade_date
        """
    )
    if daily.empty:
        return pd.DataFrame()
    daily["trade_date"] = pd.to_datetime(daily["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    daily["close"] = pd.to_numeric(daily["close"], errors="coerce")
    daily = daily.dropna(subset=["trade_date", "close"]).sort_values("trade_date").reset_index(drop=True)
    daily["shanghai_prev19_mean"] = daily["close"].rolling(19, min_periods=19).mean()
    next_date = {trade_dates[i]: trade_dates[i + 1] for i in range(len(trade_dates) - 1)}
    daily["entry_date"] = daily["trade_date"].map(next_date)
    ma = daily[["entry_date", "shanghai_prev19_mean"]].dropna(subset=["entry_date", "shanghai_prev19_mean"]).copy()

    minutes = ch.query_df(
        f"""
        SELECT datetime, close
        FROM {table}
        WHERE code = '999999.SH'
          AND datetime >= toDateTime('{start_date} 09:30:00')
          AND datetime <= toDateTime('{end_date} 15:00:00')
        ORDER BY datetime
        """
    )
    if minutes.empty:
        return pd.DataFrame()
    minutes["confirm_datetime"] = pd.to_datetime(minutes["datetime"], errors="coerce")
    minutes["entry_date"] = minutes["confirm_datetime"].dt.strftime("%Y-%m-%d")
    minutes["shanghai_intraday_close"] = pd.to_numeric(minutes["close"], errors="coerce")
    gate = minutes[["entry_date", "confirm_datetime", "shanghai_intraday_close"]].merge(ma, on="entry_date", how="left")
    gate["shanghai_live_ma20"] = (gate["shanghai_prev19_mean"] * 19.0 + gate["shanghai_intraday_close"]) / 20.0
    gate["market_gate_pass"] = gate["shanghai_intraday_close"] > gate["shanghai_live_ma20"]
    gate["market_gate"] = "shanghai_intraday_close_gt_live_ma20"
    return gate.dropna(subset=["confirm_datetime", "entry_date", "shanghai_live_ma20"]).reset_index(drop=True)


def _load_signal_bars(codes: list[str], start_date: str, end_date: str, period: int) -> pd.DataFrame:
    if not codes:
        return pd.DataFrame()
    table = {15: "kline_minute_15", 30: "kline_minute_30"}[int(period)]
    quoted = ", ".join([f"'{code}'" for code in sorted(set(codes))])
    ch = clickhouse_client()
    df = ch.query_df(
        f"""
        SELECT code, datetime, open, high, low, close, volume, amount
        FROM {table}
        WHERE code IN ({quoted})
          AND datetime >= toDateTime('{start_date} 09:30:00')
          AND datetime <= toDateTime('{end_date} 15:00:00')
        ORDER BY code, datetime
        """
    )
    if df.empty:
        return df
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    df["entry_date"] = df["datetime"].dt.strftime("%Y-%m-%d")
    df["bar_time"] = df["datetime"].dt.strftime("%H:%M:%S")
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["amount"] = df["amount"].where(df["amount"] > 0, df["volume"] * df["close"])
    df = df.dropna(subset=["code", "datetime", "open", "high", "low", "close", "amount"]).copy()
    group = df.groupby(["code", "entry_date"], sort=False)
    df["prev_intraday_high"] = group["high"].cummax().groupby([df["code"], df["entry_date"]]).shift(1)
    df["prev_bar_high"] = group["high"].shift(1)
    df["amount_ma5_prev"] = df.groupby("code")["amount"].transform(lambda s: s.shift(1).rolling(5, min_periods=3).mean())
    first2 = group.cumcount() < 2
    open_range = df[first2].groupby(["code", "entry_date"])["high"].max().rename("open_range_high").reset_index()
    df = df.merge(open_range, on=["code", "entry_date"], how="left")
    return df.reset_index(drop=True)


def _apply_trigger(joined: pd.DataFrame, trigger: TriggerProfile) -> pd.DataFrame:
    d = joined[joined["bar_time"] >= trigger.min_time].copy()
    d = d[d["amount_ma5_prev"] > 0].copy()
    vol_ok = d["amount"] >= d["amount_ma5_prev"] * trigger.vol_mult
    if trigger.trigger_type == "break_day_high":
        price_ok = d["close"] > d["prev_intraday_high"]
    elif trigger.trigger_type == "break_open_range_high":
        price_ok = d["close"] > d["open_range_high"]
    elif trigger.trigger_type == "break_prev_bar_high":
        price_ok = d["close"] > d["prev_bar_high"]
    else:
        raise ValueError(f"Unsupported trigger type: {trigger.trigger_type}")
    d = d[vol_ok & price_ok].copy()
    if d.empty:
        return d
    d = d.sort_values(["entry_date", "code", "datetime"]).groupby(["candidate_profile", "entry_date", "code"], as_index=False).first()
    d["trigger_profile"] = trigger.name
    d["pattern"] = d["candidate_profile"] + "__" + d["trigger_profile"]
    d["trigger_type"] = trigger.trigger_type
    d["confirm_datetime"] = d["datetime"]
    d["entry_price"] = d["close"]
    d["confirm_amount"] = d["amount"]
    d["confirm_amount_ma5_prev"] = d["amount_ma5_prev"]
    d["volume_ratio"] = d["confirm_amount"] / d["confirm_amount_ma5_prev"]
    return d


def _build_signals(events: pd.DataFrame, output_dir: Path, start_date: str, end_date: str, period: int, trade_dates: list[str]) -> pd.DataFrame:
    candidates = pd.concat([_select_candidates(events, p) for p in _candidate_profiles()], ignore_index=True)
    candidates.to_csv(output_dir / "candidates.csv", index=False, encoding="utf-8-sig")
    if candidates.empty:
        return pd.DataFrame()
    bars = _load_signal_bars(candidates["code"].dropna().astype(str).unique().tolist(), start_date, end_date, period)
    joined = candidates.merge(bars, on=["code", "entry_date"], how="inner", suffixes=("", "_bar"))
    signal_frames = [_apply_trigger(joined, t) for t in _trigger_profiles()]
    signals = pd.concat([x for x in signal_frames if not x.empty], ignore_index=True) if signal_frames else pd.DataFrame()
    if signals.empty:
        return signals
    gate = _load_intraday_shanghai_gate(start_date, end_date, period, trade_dates)
    if gate.empty:
        signals = signals.iloc[0:0].copy()
    else:
        signals = signals.merge(gate, on=["entry_date", "confirm_datetime"], how="left")
        before_gate_count = len(signals)
        signals = signals[signals["market_gate_pass"].fillna(False)].copy()
        gate_summary = {
            "before_market_gate": int(before_gate_count),
            "after_market_gate": int(len(signals)),
            "market_gate": "shanghai_intraday_close_gt_live_ma20",
            "period": f"{int(period)}m",
        }
        (output_dir / "market_gate_summary.json").write_text(json.dumps(gate_summary, ensure_ascii=False, indent=2), encoding="utf-8")
    if signals.empty:
        signals.to_csv(output_dir / "signals.csv", index=False, encoding="utf-8-sig")
        return signals
    keep = [
        "trade_date",
        "entry_date",
        "code",
        "name",
        "v4_rank",
        "v4_score",
        "entry_price",
        "confirm_datetime",
        "confirm_amount",
        "confirm_amount_ma5_prev",
        "volume_ratio",
        "candidate_profile",
        "trigger_profile",
        "pattern",
        "trigger_type",
        "g2_open_state",
        "mom5",
        "mom10",
        "mom20",
        "vol_ratio",
        "vol10",
        "dist_ma20",
        "stock_ma20_slope5",
        "market_gate",
        "shanghai_intraday_close",
        "shanghai_live_ma20",
        "shanghai_prev19_mean",
    ]
    signals = signals[keep].sort_values(["entry_date", "confirm_datetime", "v4_rank", "code"]).reset_index(drop=True)
    signals.to_parquet(output_dir / "signals.parquet", index=False)
    signals.to_csv(output_dir / "signals.csv", index=False, encoding="utf-8-sig")
    return signals


def _sort_signals(signals: pd.DataFrame, sort_mode: str) -> pd.DataFrame:
    d = signals.copy()
    if sort_mode == "rank":
        cols = ["entry_date", "v4_rank", "v4_score", "confirm_datetime", "code"]
        asc = [True, True, False, True, True]
    elif sort_mode == "volume_ratio":
        cols = ["entry_date", "volume_ratio", "v4_rank", "confirm_datetime", "code"]
        asc = [True, False, True, True, True]
    else:
        cols = ["entry_date", "confirm_datetime", "v4_rank", "v4_score", "code"]
        asc = [True, True, True, False, True]
    return d.sort_values(cols, ascending=asc, na_position="last")


def _run_backtest(
    signals: pd.DataFrame,
    output_dir: Path,
    start_date: str,
    end_date: str,
    period: int,
    sort_mode: str = "trigger_time",
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    trade_dates = _load_trade_dates(start_date, end_date)
    date_idx = {date: i for i, date in enumerate(trade_dates)}
    signals = signals.copy()
    signals["entry_date"] = pd.to_datetime(signals["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    signals["confirm_datetime"] = pd.to_datetime(signals["confirm_datetime"], errors="coerce")
    signals = signals[signals["entry_date"].isin(date_idx)].dropna(subset=["code", "entry_price", "confirm_datetime"]).copy()
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
    bars = _load_minute_bars(codes, start_date, end_date, period)
    bars_by_code_date = {
        (code, date): g.sort_values("datetime").copy()
        for (code, date), g in bars.groupby(["code", "bar_date"], sort=False)
    }
    if int(period) == 15:
        profile = FillProfile("gap_confirm_15m_close__intraday_15m_close", gap_open_mode="confirm_15m", intraday_mode="bar_close_15m")
    else:
        profile = FillProfile("gap_confirm_30m_close__intraday_30m_close", gap_open_mode="confirm_30m", intraday_mode="bar_close")

    cash = 150000.0
    lots: List[Lot] = []
    trades: List[Dict[str, Any]] = []
    curve: List[Dict[str, Any]] = []

    for date in trade_dates:
        next_lots: List[Lot] = []
        for lot in lots:
            day_bars = bars_by_code_date.get((lot.code, date), pd.DataFrame())
            if not day_bars.empty:
                day_bars = day_bars[day_bars["datetime"] > lot.buy_datetime].copy()
            lot_after, cash = _process_lot_exits(
                lot=lot,
                bars30=day_bars,
                semantic_bars=day_bars,
                cash=cash,
                trades=trades,
                prev_low=prev_low_map.get((lot.code, date)),
                profile=profile,
                gap_confirm_bars=day_bars,
                sell_slippage_bps=5.0,
                commission_bps=2.5,
                stamp_tax_bps=5.0,
            )
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

        today = signals_by_date.get(date)
        if today is not None and not today.empty:
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
                target_capital = sizing_equity * 0.50
                for row in candidates.itertuples(index=False):
                    capital_each = min(cash, target_capital)
                    if capital_each <= 0:
                        break
                    buy_price = float(getattr(row, "entry_price")) * 1.0005
                    if buy_price <= 0 or not np.isfinite(buy_price):
                        continue
                    entry_idx = date_idx[date]
                    sell_idx = min(entry_idx + 10, len(trade_dates) - 1)
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

        market_value = 0.0
        for lot in lots:
            close = _safe_price(price_map, lot.code, date) or lot.buy_price
            market_value += lot.shares * close
        equity = cash + market_value
        curve.append({"date": date, "cash": cash, "market_value": market_value, "equity": equity, "holding_count": len(lots)})

    curve_df = pd.DataFrame(curve)
    trades_df = pd.DataFrame(trades)
    if int(period) == 15 and not trades_df.empty and "exit_reason" in trades_df.columns:
        trades_df["exit_reason"] = trades_df["exit_reason"].replace(
            {
                "stop_loss_30m": "stop_loss_15m",
                "take_profit_partial_30m": "take_profit_partial_15m",
            }
        )
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
        "start_date": start_date,
        "end_date": end_date,
        "period": f"{int(period)}m",
        "sort_mode": sort_mode,
        "exit_profile": profile.__dict__,
        "signal_count": int(len(signals)),
        "trade_count": int(len(trades_df)),
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
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return summary


def _write_report(output_dir: Path, rows: list[dict[str, Any]], period: int) -> None:
    df = pd.DataFrame(rows).sort_values(["total_return", "max_drawdown"], ascending=[False, False])
    df.to_csv(output_dir / "summary.csv", index=False, encoding="utf-8-sig")
    lines = [
        "# G2 V2 Trend Continuation Initial Validation",
        "",
        f"All entries use previous-day candidate context plus signal-day {int(period)}m visible confirmation.",
        "",
        "| combo | signals | trades | total | excess | max_dd | win | avg_trade | exits |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in df.to_dict("records"):
        lines.append(
            f"| {row['combo']} | {int(row.get('signal_count') or 0)} | {int(row.get('trade_count') or 0)} | {_pct(row.get('total_return'))} | {_pct(row.get('excess_return'))} | {_pct(row.get('max_drawdown'))} | {_pct(row.get('win_rate'))} | {_pct(row.get('avg_trade_return'))} | {row.get('exit_reason_counts') or ''} |"
        )
    (output_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


def _write_signal_event_study(signals: pd.DataFrame, output_dir: Path, start_date: str, end_date: str) -> None:
    if signals.empty:
        pd.DataFrame().to_csv(output_dir / "signal_event_study.csv", index=False, encoding="utf-8-sig")
        return
    codes = signals["code"].dropna().astype(str).unique().tolist()
    daily = _load_daily_ohlc(codes, start_date, end_date)
    if daily.empty:
        pd.DataFrame().to_csv(output_dir / "signal_event_study.csv", index=False, encoding="utf-8-sig")
        return
    daily = daily.sort_values(["code", "trade_date"]).copy()
    daily["idx"] = daily.groupby("code").cumcount()
    keyed = daily[["code", "trade_date", "idx"]].rename(columns={"trade_date": "entry_date", "idx": "entry_idx"})
    study = signals.merge(keyed, on=["code", "entry_date"], how="left")
    daily_by_code = {code: g.reset_index(drop=True).copy() for code, g in daily.groupby("code", sort=False)}
    rows: list[dict[str, Any]] = []
    for row in study.itertuples(index=False):
        code = str(getattr(row, "code"))
        entry_idx = getattr(row, "entry_idx")
        entry_price = float(getattr(row, "entry_price"))
        if not np.isfinite(entry_price) or entry_price <= 0 or pd.isna(entry_idx):
            continue
        hist = daily_by_code.get(code)
        if hist is None or hist.empty:
            continue
        base_idx = int(entry_idx)
        item: dict[str, Any] = {
            "pattern": getattr(row, "pattern"),
            "candidate_profile": getattr(row, "candidate_profile"),
            "trigger_profile": getattr(row, "trigger_profile"),
            "code": code,
            "entry_date": getattr(row, "entry_date"),
            "entry_price": entry_price,
            "v4_rank": getattr(row, "v4_rank"),
            "volume_ratio": getattr(row, "volume_ratio"),
        }
        for horizon in (1, 2, 3, 5, 10):
            window = hist[(hist["idx"] > base_idx) & (hist["idx"] <= base_idx + horizon)].copy()
            if window.empty:
                item[f"fwd_ret_{horizon}d"] = np.nan
                item[f"mfe_{horizon}d"] = np.nan
                item[f"mae_{horizon}d"] = np.nan
            else:
                last_close = float(window["close"].iloc[-1])
                item[f"fwd_ret_{horizon}d"] = last_close / entry_price - 1.0
                item[f"mfe_{horizon}d"] = float(window["high"].max()) / entry_price - 1.0
                item[f"mae_{horizon}d"] = float(window["low"].min()) / entry_price - 1.0
        rows.append(item)
    detail = pd.DataFrame(rows)
    detail.to_csv(output_dir / "signal_event_study_detail.csv", index=False, encoding="utf-8-sig")
    if detail.empty:
        pd.DataFrame().to_csv(output_dir / "signal_event_study.csv", index=False, encoding="utf-8-sig")
        return
    agg_rows: list[dict[str, Any]] = []
    for pattern, g in detail.groupby("pattern", sort=True):
        agg: dict[str, Any] = {"pattern": pattern, "signal_count": int(len(g))}
        for horizon in (1, 2, 3, 5, 10):
            ret = pd.to_numeric(g[f"fwd_ret_{horizon}d"], errors="coerce")
            mfe = pd.to_numeric(g[f"mfe_{horizon}d"], errors="coerce")
            mae = pd.to_numeric(g[f"mae_{horizon}d"], errors="coerce")
            agg[f"avg_ret_{horizon}d"] = float(ret.mean()) if not ret.dropna().empty else np.nan
            agg[f"win_{horizon}d"] = float((ret > 0).mean()) if not ret.dropna().empty else np.nan
            agg[f"avg_mfe_{horizon}d"] = float(mfe.mean()) if not mfe.dropna().empty else np.nan
            agg[f"avg_mae_{horizon}d"] = float(mae.mean()) if not mae.dropna().empty else np.nan
        agg_rows.append(agg)
    pd.DataFrame(agg_rows).sort_values("avg_ret_5d", ascending=False).to_csv(
        output_dir / "signal_event_study.csv",
        index=False,
        encoding="utf-8-sig",
    )


def run(event_dataset: Path, state_daily: Path, output_dir: Path, start_date: str, end_date: str, period: int) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    trade_dates = _load_trade_dates("2010-01-01", end_date)
    events = _prepare_events(event_dataset, state_daily, start_date, end_date, trade_dates)
    signals = _build_signals(events, output_dir, start_date, end_date, period, trade_dates)
    rows: list[dict[str, Any]] = []
    if not signals.empty:
        _write_signal_event_study(signals, output_dir, start_date, end_date)
        for combo, combo_signals in signals.groupby("pattern", sort=True):
            summary = _run_backtest(
                signals=combo_signals.copy(),
                output_dir=output_dir / "runs" / combo,
                start_date=start_date,
                end_date=end_date,
                period=period,
                sort_mode="trigger_time",
            )
            summary["combo"] = combo
            rows.append(summary)
    _write_report(output_dir, rows, period)
    payload = {
        "schema_version": 1,
        "strategy_name": "G2 V2: strong-not-overheated trend continuation",
        "start_date": start_date,
        "end_date": end_date,
        "period": f"{int(period)}m",
        "market_gate": "shanghai_intraday_close_gt_live_ma20",
        "candidate_profiles": [p.__dict__ for p in _candidate_profiles()],
        "trigger_profiles": [p.__dict__ for p in _trigger_profiles()],
        "candidate_count": int(len(pd.read_csv(output_dir / "candidates.csv"))) if (output_dir / "candidates.csv").exists() else 0,
        "signal_count": int(len(signals)),
        "rows": rows,
        "outputs": {
            "candidates": "candidates.csv",
            "signals": "signals.csv",
            "summary": "summary.csv",
            "report": "report.md",
            "runs": "runs/",
        },
        "notes": [
            "Candidate context uses T-1 individual-stock daily data only; G2 NORMAL is not required.",
            "Market timing gate uses the Shanghai Composite intraday close at confirm_datetime against a live daily MA20 built from the previous 19 daily closes plus the current intraday close.",
            f"Entry confirmation uses only signal-day {int(period)}m bars at or before confirm_datetime.",
            f"Portfolio engine uses max 2 positions, max 1 buy per day, max 50% per stock, {int(period)}m -5% stop, +10% half take-profit, 10-trading-day time exit, and previous-day-low {int(period)}m-close confirmation exit after half take-profit.",
        ],
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate G2 V2 strong-not-overheated trend continuation.")
    parser.add_argument("--event-dataset", default=str(DEFAULT_EVENT_DATASET))
    parser.add_argument("--state-daily", default=str(DEFAULT_STATE_DAILY))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--start-date", default="2024-07-09")
    add_end_date_argument(parser)
    parser.add_argument("--period", type=int, choices=[15, 30], default=15)
    args = parser.parse_args()
    payload = run(
        event_dataset=Path(args.event_dataset),
        state_daily=Path(args.state_daily),
        output_dir=Path(args.output_dir),
        start_date=str(args.start_date),
        end_date=resolve_end_date(args.end_date),
        period=int(args.period),
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()

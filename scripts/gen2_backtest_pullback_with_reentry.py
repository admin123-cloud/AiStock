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

from api.gen2_strategy import GEN2_OPEN_RULE_V1  # noqa: E402
from scripts.gen2_backtest_open_v1_intraday_risk import Lot, _sell_trade  # noqa: E402
from scripts.gen2_backtest_open_v1_portfolio import (  # noqa: E402
    _json_default,
    _load_benchmark,
    _load_trade_dates,
    _safe_price,
    _sort_signals,
    _pct,
)
from scripts.gen2_compare_prev_low_exit_fills import FillProfile, _load_minute_bars  # noqa: E402
from scripts.gen2_sweep_open_v1_semantic_exits import _load_daily_ohlc  # noqa: E402
from scripts.gen2_sweep_pullback_factor_filters import _load_labeled_signals  # noqa: E402
from scripts.gen2_sweep_pullback_profit_extension import _process_lot_exits  # noqa: E402
from scripts.gen2_sweep_pullback_risk_layers import _quality_mask  # noqa: E402

DEFAULT_LABELED_SIGNALS = REPO_ROOT / "reports" / "gen2_v4_factor_profile" / "pullback_signal_factor_labeled.parquet"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "reports" / "gen2_pullback_with_reentry"


@dataclass(frozen=True)
class PortfolioProfile:
    key: str
    note: str
    enable_reentry: bool = False
    reentry_window_days: int = 5
    reentry_capital_ratio: float = 0.5
    reentry_hold_days: int = 10
    reentry_priority: str = "before_new_signal"
    reentry_market_gate: str = "none"


@dataclass
class ReentryWatch:
    code: str
    name: str
    source_sell_datetime: pd.Timestamp
    source_sell_date: str
    expire_date: str
    threshold_high: float
    capital: float
    v4_rank: int
    v4_score: float


def _load_signals(path: Path, start_date: str, end_date: str) -> pd.DataFrame:
    signals = _load_labeled_signals(path, start_date, end_date)
    rule = GEN2_OPEN_RULE_V1
    d = signals[
        (signals["pattern"] == rule["pattern"])
        & (signals["g2_open_state"] == rule["g2_open_state"])
        & (signals["trigger_type"] == rule["trigger_type"])
    ].copy()
    d["quality_confirm"] = _quality_mask(d).fillna(False).astype(bool)
    d["capital_weight"] = 1.0
    d["confirm_datetime"] = pd.to_datetime(d["confirm_datetime"], errors="coerce")
    d["v4_rank"] = pd.to_numeric(d["v4_rank"], errors="coerce").fillna(999).astype(int)
    d["v4_score"] = pd.to_numeric(d["v4_score"], errors="coerce").fillna(0.0)
    d["entry_price"] = pd.to_numeric(d["entry_price"], errors="coerce")
    return d.dropna(subset=["entry_date", "code", "entry_price", "confirm_datetime"]).reset_index(drop=True)


def _add_bar_features(bars: pd.DataFrame) -> pd.DataFrame:
    if bars.empty:
        return bars
    d = bars.sort_values(["code", "datetime"]).copy()
    g = d.groupby("code", sort=False)
    d["volume_ma5_prev"] = g["volume"].transform(lambda s: s.shift(1).rolling(5, min_periods=5).mean())
    return d


def _load_reentry_market_gate(start_date: str, end_date: str, trade_dates: list[str], gate: str) -> dict[str, bool]:
    if gate == "none":
        return {date: True for date in trade_dates}
    if gate != "csi1000_prev_ma20_slope5":
        raise ValueError(f"Unknown reentry market gate: {gate}")

    from utils.market_warehouse import clickhouse_client

    start_ts = (pd.Timestamp(start_date) - pd.Timedelta(days=80)).strftime("%Y-%m-%d")
    ch = clickhouse_client()
    df = ch.query_df(
        f"""
        SELECT trade_date, close
        FROM kline_daily
        WHERE code = '000852.SH'
          AND trade_date BETWEEN '{start_ts}' AND '{end_date}'
        ORDER BY trade_date
        """
    )
    if df.empty:
        return {date: False for date in trade_dates}
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["trade_date", "close"]).sort_values("trade_date").reset_index(drop=True)
    df["ma20"] = df["close"].rolling(20, min_periods=20).mean()
    df["ma20_slope5"] = df["ma20"] / df["ma20"].shift(5) - 1.0
    df["strong"] = (df["close"] > df["ma20"]) & (df["ma20_slope5"] > 0)
    strong_by_date = dict(zip(df["trade_date"], df["strong"].fillna(False).astype(bool)))

    out: dict[str, bool] = {}
    for idx, date in enumerate(trade_dates):
        if idx == 0:
            out[date] = False
            continue
        prev_date = trade_dates[idx - 1]
        out[date] = bool(strong_by_date.get(prev_date, False))
    return out


def _make_watch(
    trade: dict[str, Any],
    bars_by_code_date: dict[tuple[str, str], pd.DataFrame],
    trade_dates: list[str],
    profile: PortfolioProfile,
) -> Optional[ReentryWatch]:
    reason = str(trade.get("exit_reason", ""))
    if reason not in {"weak_prev_day_low_break_30m_close", "weak_prev_day_low_gap_confirm_30m_close"}:
        return None
    sell_dt = pd.Timestamp(trade["sell_datetime"])
    sell_date = sell_dt.strftime("%Y-%m-%d")
    if sell_date not in trade_dates:
        return None
    code = str(trade["code"])
    day_bars = bars_by_code_date.get((code, sell_date), pd.DataFrame())
    visible = day_bars[pd.to_datetime(day_bars["datetime"]) <= sell_dt].copy() if not day_bars.empty else pd.DataFrame()
    if visible.empty:
        return None
    threshold_high = float(visible["high"].max())
    expire_idx = min(trade_dates.index(sell_date) + int(profile.reentry_window_days), len(trade_dates) - 1)
    return ReentryWatch(
        code=code,
        name=str(trade["name"]),
        source_sell_datetime=sell_dt,
        source_sell_date=sell_date,
        expire_date=trade_dates[expire_idx],
        threshold_high=threshold_high,
        capital=float(trade["capital"]) * float(profile.reentry_capital_ratio),
        v4_rank=int(float(trade["v4_rank"])),
        v4_score=float(trade["v4_score"]),
    )


def _find_reentry_bar(watch: ReentryWatch, bars: pd.DataFrame, date: str) -> Optional[pd.Series]:
    day = bars[(bars["code"] == watch.code) & (bars["bar_date"] == date)].copy()
    if day.empty:
        return None
    scan = day[day["datetime"] > watch.source_sell_datetime].copy()
    if scan.empty:
        return None
    vol_ok = pd.to_numeric(scan["volume"], errors="coerce") > pd.to_numeric(scan["volume_ma5_prev"], errors="coerce")
    mask = (pd.to_numeric(scan["close"], errors="coerce") > float(watch.threshold_high)) & vol_ok
    hit = scan[mask.fillna(False)].copy()
    if hit.empty:
        return None
    return hit.iloc[0]


def _buy_lot(
    code: str,
    name: str,
    buy_datetime: pd.Timestamp,
    buy_price_raw: float,
    capital: float,
    v4_rank: int,
    v4_score: float,
    trade_dates: list[str],
    hold_days: int,
    date_idx: dict[str, int],
) -> Optional[Lot]:
    buy_date = buy_datetime.strftime("%Y-%m-%d")
    if buy_date not in date_idx:
        return None
    buy_price = float(buy_price_raw) * 1.0005
    if capital <= 0 or buy_price <= 0 or not np.isfinite(buy_price):
        return None
    entry_idx = date_idx[buy_date]
    sell_idx = min(entry_idx + int(hold_days), len(trade_dates) - 1)
    buy_fee = capital * 2.5 / 10000.0
    return Lot(
        code=code,
        name=name,
        buy_date=buy_date,
        buy_datetime=buy_datetime,
        sell_date=trade_dates[sell_idx],
        buy_price=buy_price,
        shares=(capital - buy_fee) / buy_price,
        capital=capital,
        v4_rank=int(v4_rank),
        v4_score=float(v4_score),
        peak_price=buy_price,
    )


def _profiles() -> list[PortfolioProfile]:
    return [
        PortfolioProfile("baseline", "Baseline without reentry"),
        PortfolioProfile(
            "exit_high_w5_half_reentry",
            "After previous-day-low exit, reenter half size within 5 trading days when 30m closes above high-so-far at exit with volume",
            enable_reentry=True,
            reentry_window_days=5,
            reentry_capital_ratio=0.5,
            reentry_hold_days=10,
        ),
        PortfolioProfile(
            "exit_high_w5_half_reentry_strong_market",
            "Same reentry, only when previous trading day CSI1000 close > MA20 and MA20 slope5 > 0",
            enable_reentry=True,
            reentry_window_days=5,
            reentry_capital_ratio=0.5,
            reentry_hold_days=10,
            reentry_market_gate="csi1000_prev_ma20_slope5",
        ),
    ]


def _run_profile(
    signals_path: Path,
    output_dir: Path,
    profile: PortfolioProfile,
    start_date: str,
    end_date: str,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    signals = _load_signals(signals_path, start_date, end_date)
    trade_dates = _load_trade_dates(start_date, end_date)
    if not trade_dates:
        raise RuntimeError("No trade dates available.")
    date_idx = {date: i for i, date in enumerate(trade_dates)}
    reentry_gate_by_date = _load_reentry_market_gate(start_date, end_date, trade_dates, profile.reentry_market_gate)
    signals = signals[signals["entry_date"].isin(date_idx)].copy()
    signals = _sort_signals(signals, "trigger_time").reset_index(drop=True)
    signals_by_date = {date: g.copy() for date, g in signals.groupby("entry_date", sort=True)}

    codes = signals["code"].dropna().astype(str).unique().tolist()
    daily = _load_daily_ohlc(codes, start_date, end_date)
    price_map = {(row.code, row.trade_date): float(row.close) for row in daily.itertuples(index=False)}
    prev_low_map = {
        (row.code, row.trade_date): float(row.prev_low)
        for row in daily.itertuples(index=False)
        if row.prev_low is not None and np.isfinite(row.prev_low)
    }
    bars30 = _add_bar_features(_load_minute_bars(codes, start_date, end_date, 30))
    bars30_by_code_date = {
        (code, date): g.sort_values("datetime").copy()
        for (code, date), g in bars30.groupby(["code", "bar_date"], sort=False)
    }
    fill_profile = FillProfile("gap_confirm_30m_close__intraday_30m_close", gap_open_mode="confirm_30m", intraday_mode="bar_close")

    cash = 150000.0
    lots: list[Lot] = []
    watches: list[ReentryWatch] = []
    trades: list[dict[str, Any]] = []
    curve: list[dict[str, Any]] = []
    reentry_buy_count = 0

    for date in trade_dates:
        bought_today = False
        next_lots: list[Lot] = []
        for lot in lots:
            before_count = len(trades)
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
                fill_profile=fill_profile,
                gap_confirm_bars=day_bars30,
                semantic_mode="prev_low",
                trailing_after_tp=None,
            )
            new_trades = trades[before_count:]
            if profile.enable_reentry:
                for trade in new_trades:
                    watch = _make_watch(trade, bars30_by_code_date, trade_dates, profile)
                    if watch is not None and watch.capital > 0:
                        watches.append(watch)
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

        if profile.enable_reentry and not bought_today:
            active_watches = []
            held_codes = {lot.code for lot in lots}
            for watch in watches:
                if date > watch.expire_date:
                    continue
                if not reentry_gate_by_date.get(date, False):
                    active_watches.append(watch)
                    continue
                if watch.code in held_codes:
                    active_watches.append(watch)
                    continue
                if len(lots) >= 2 or cash <= 0:
                    active_watches.append(watch)
                    continue
                bar = _find_reentry_bar(watch, bars30, date)
                if bar is None:
                    active_watches.append(watch)
                    continue
                capital = min(cash, float(watch.capital))
                lot = _buy_lot(
                    code=watch.code,
                    name=watch.name,
                    buy_datetime=pd.Timestamp(bar["datetime"]),
                    buy_price_raw=float(bar["close"]),
                    capital=capital,
                    v4_rank=watch.v4_rank,
                    v4_score=watch.v4_score,
                    trade_dates=trade_dates,
                    hold_days=profile.reentry_hold_days,
                    date_idx=date_idx,
                )
                if lot is None:
                    active_watches.append(watch)
                    continue
                lot.reentry_source_sell_datetime = watch.source_sell_datetime.strftime("%Y-%m-%d %H:%M:%S")
                cash -= capital
                lots.append(lot)
                bought_today = True
                reentry_buy_count += 1
                break
            watches = active_watches

        today = signals_by_date.get(date)
        if today is not None and not today.empty and not bought_today:
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
                target_capital = min(sizing_equity / 2.0, sizing_equity * 0.50)
                for row in candidates.itertuples(index=False):
                    capital = min(cash, target_capital)
                    lot = _buy_lot(
                        code=str(getattr(row, "code")),
                        name=str(getattr(row, "name")),
                        buy_datetime=getattr(row, "confirm_datetime"),
                        buy_price_raw=float(getattr(row, "entry_price")),
                        capital=capital,
                        v4_rank=int(getattr(row, "v4_rank")),
                        v4_score=float(getattr(row, "v4_score")),
                        trade_dates=trade_dates,
                        hold_days=10,
                        date_idx=date_idx,
                    )
                    if lot is None:
                        continue
                    cash -= capital
                    lots.append(lot)
                    bought_today = True
                    break

        market_value = 0.0
        for lot in lots:
            close = _safe_price(price_map, lot.code, date) or lot.buy_price
            market_value += lot.shares * close
        equity = cash + market_value
        curve.append({"date": date, "cash": cash, "market_value": market_value, "equity": equity, "holding_count": len(lots), "watch_count": len(watches)})

    curve_df = pd.DataFrame(curve)
    trades_df = pd.DataFrame(trades)
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
        "rule": GEN2_OPEN_RULE_V1,
        "profile": profile.__dict__,
        "start_date": start_date,
        "end_date": end_date,
        "signal_count": int(len(signals)),
        "trade_count": int(len(trades_df)),
        "reentry_buy_count": int(reentry_buy_count),
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


def _write_report(output_dir: Path, rows: list[dict[str, Any]]) -> None:
    lines = [
        "# G2 Pullback Portfolio With Reentry",
        "",
        "Portfolio-level test with cash, max 2 positions and max 1 buy per day.",
        "Reentry watches only previous-day-low exits and uses a no-lookahead exit-day high-so-far threshold.",
        "The strong-market profile uses previous trading day's CSI1000 close > MA20 and MA20 slope5 > 0 as a no-lookahead gate.",
        "",
        "| profile | reentries | trades | total | excess | max_dd | win | avg_trade | note |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in rows:
        lines.append(
            f"| {row['key']} | {int(row.get('reentry_buy_count') or 0)} | {int(row.get('trade_count') or 0)} | "
            f"{_pct(row.get('total_return'))} | {_pct(row.get('excess_return'))} | {_pct(row.get('max_drawdown'))} | "
            f"{_pct(row.get('win_rate'))} | {_pct(row.get('avg_trade_return'))} | {row.get('note', '')} |"
        )
    (output_dir / "findings.md").write_text("\n".join(lines), encoding="utf-8")


def run(signals: Path, output_dir: Path, start_date: str, end_date: str, profile_keys: set[str] | None = None) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    profiles = _profiles()
    if profile_keys:
        missing = sorted(profile_keys - {p.key for p in profiles})
        if missing:
            raise ValueError(f"Unknown profiles: {missing}")
        profiles = [p for p in profiles if p.key in profile_keys]
    rows: list[dict[str, Any]] = []
    for profile in profiles:
        summary = _run_profile(signals, output_dir / profile.key, profile, start_date, end_date)
        summary["key"] = profile.key
        summary["note"] = profile.note
        rows.append(summary)
    summary_df = pd.DataFrame(rows).sort_values("total_return", ascending=False)
    summary_df.to_csv(output_dir / "summary.csv", index=False, encoding="utf-8-sig")
    payload = {
        "schema_version": 1,
        "start_date": start_date,
        "end_date": end_date,
        "signals": str(signals),
        "rows": summary_df.replace({np.nan: None}).to_dict("records"),
        "outputs": {"summary": "summary.csv", "report": "findings.md"},
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    _write_report(output_dir, summary_df.to_dict("records"))
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Portfolio-level G2 pullback backtest with previous-low reentry.")
    parser.add_argument("--signals", default=str(DEFAULT_LABELED_SIGNALS))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--start-date", default="2024-07-09")
    parser.add_argument("--end-date", default="2026-05-21")
    parser.add_argument("--profiles", default="", help="Comma-separated profile keys. Empty means all profiles.")
    args = parser.parse_args()
    keys = {item.strip() for item in str(args.profiles).split(",") if item.strip()} if str(args.profiles).strip() else None
    payload = run(Path(args.signals), Path(args.output_dir), str(args.start_date), str(args.end_date), profile_keys=keys)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()

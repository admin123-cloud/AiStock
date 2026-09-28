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
from scripts.gen2_compare_prev_low_exit_fills import FillProfile, _load_minute_bars, _prev_low_exit_from_bars  # noqa: E402
from scripts.gen2_sweep_open_v1_semantic_exits import _load_daily_ohlc  # noqa: E402
from scripts.gen2_sweep_pullback_factor_filters import _load_labeled_signals  # noqa: E402
from scripts.gen2_sweep_pullback_risk_layers import _quality_mask  # noqa: E402

DEFAULT_LABELED_SIGNALS = REPO_ROOT / "reports" / "gen2_v4_factor_profile" / "pullback_signal_factor_labeled.parquet"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "reports" / "gen2_pullback_profit_extension_sweep"


@dataclass(frozen=True)
class ExtensionProfile:
    key: str
    note: str
    strong_hold_days: int = 10
    normal_hold_days: int = 10
    strong_semantic: str = "prev_low"
    normal_semantic: str = "prev_low"
    strong_trailing_after_tp: Optional[float] = None
    normal_trailing_after_tp: Optional[float] = None


def _load_signals(path: Path, start_date: str, end_date: str) -> pd.DataFrame:
    df = pd.read_parquet(path)
    rule = GEN2_OPEN_RULE_V1
    d = df[
        (df["pattern"] == rule["pattern"])
        & (df["g2_open_state"] == rule["g2_open_state"])
        & (df["trigger_type"] == rule["trigger_type"])
    ].copy()
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    d = d[(d["entry_date"] >= start_date) & (d["entry_date"] <= end_date)].copy()
    d["confirm_datetime"] = pd.to_datetime(d["confirm_datetime"], errors="coerce")
    d["v4_rank"] = pd.to_numeric(d["v4_rank"], errors="coerce").fillna(999).astype(int)
    d["v4_score"] = pd.to_numeric(d["v4_score"], errors="coerce").fillna(0.0)
    d["entry_price"] = pd.to_numeric(d["entry_price"], errors="coerce")
    d["confirm_amount"] = pd.to_numeric(d.get("confirm_amount"), errors="coerce")
    d["confirm_amount_ma5_prev"] = pd.to_numeric(d.get("confirm_amount_ma5_prev"), errors="coerce")
    d["volume_ratio"] = np.where(d["confirm_amount_ma5_prev"] > 0, d["confirm_amount"] / d["confirm_amount_ma5_prev"], np.nan)
    if "quality_confirm" not in d.columns:
        d["quality_confirm"] = False
    d["quality_confirm"] = d["quality_confirm"].fillna(False).astype(bool)
    if "capital_weight" not in d.columns:
        d["capital_weight"] = 1.0
    d["capital_weight"] = pd.to_numeric(d["capital_weight"], errors="coerce").fillna(1.0).clip(0.0, 1.0)
    return d.dropna(subset=["entry_date", "code", "entry_price", "confirm_datetime"]).reset_index(drop=True)


def _strong_mask(signals: pd.DataFrame) -> pd.Series:
    quality = signals["quality_confirm"].fillna(False).astype(bool)
    rank50 = pd.to_numeric(signals["v4_rank"], errors="coerce") <= 50
    return quality | rank50


def _prepare_signal_source(labeled_signals: Path, output_dir: Path, start_date: str, end_date: str) -> Path:
    signals = _load_labeled_signals(labeled_signals, start_date, end_date)
    signals["quality_confirm"] = _quality_mask(signals).fillna(False).astype(bool)
    signals["capital_weight"] = 1.0
    path = output_dir / "_prepared_signals.parquet"
    signals.to_parquet(path, index=False)
    signals.to_csv(output_dir / "_prepared_signals.csv", index=False, encoding="utf-8-sig")
    return path


def _process_lot_exits(
    lot: Lot,
    bars30: pd.DataFrame,
    semantic_bars: pd.DataFrame,
    cash: float,
    trades: List[Dict[str, Any]],
    prev_low: Optional[float],
    fill_profile: FillProfile,
    gap_confirm_bars: Optional[pd.DataFrame],
    semantic_mode: str,
    trailing_after_tp: Optional[float],
) -> tuple[Optional[Lot], float]:
    if bars30.empty and semantic_bars.empty:
        return lot, cash

    events: list[dict[str, Any]] = []
    stop_price = lot.buy_price * 0.95
    take_price = lot.buy_price * 1.10

    for bar in bars30.itertuples(index=False):
        dt = pd.Timestamp(getattr(bar, "datetime"))
        high = float(getattr(bar, "high"))
        low = float(getattr(bar, "low"))
        close = float(getattr(bar, "close"))
        lot.peak_price = max(lot.peak_price, high)
        if low <= stop_price:
            events.append({"dt": dt, "price": stop_price, "reason": "stop_loss_30m", "ratio": 1.0, "priority": 1})
            break
        if not lot.take_profit_done and high >= take_price:
            events.append({"dt": dt, "price": take_price, "reason": "take_profit_partial_30m", "ratio": 0.5, "priority": 2})
            break
        if lot.take_profit_done and trailing_after_tp is not None and lot.peak_price > 0:
            trail_price = lot.peak_price * (1.0 - abs(float(trailing_after_tp)))
            if low <= trail_price:
                events.append({"dt": dt, "price": trail_price, "reason": "profit_trailing_stop_30m", "ratio": 1.0, "priority": 3})
                break
        lot.peak_price = max(lot.peak_price, close)

    if semantic_mode == "prev_low":
        semantic_dt, semantic_price, semantic_reason = _prev_low_exit_from_bars(
            lot,
            semantic_bars,
            prev_low,
            fill_profile,
            gap_confirm_bars=gap_confirm_bars,
        )
        if semantic_dt is not None and semantic_price is not None and semantic_reason is not None:
            events.append({"dt": semantic_dt, "price": semantic_price, "reason": semantic_reason, "ratio": 1.0, "priority": 4})

    if not events:
        return lot, cash
    event = sorted(events, key=lambda item: (item["dt"], item["priority"]))[0]
    trade = _sell_trade(
        lot=lot,
        sell_datetime=event["dt"],
        raw_price=float(event["price"]),
        ratio=float(event["ratio"]),
        reason=str(event["reason"]),
        sell_slippage_bps=5.0,
        commission_bps=2.5,
        stamp_tax_bps=5.0,
    )
    cash += float(trade["proceeds"])
    trades.append({k: v for k, v in trade.items() if k != "proceeds"})
    lot.shares -= float(trade["shares"])
    lot.capital -= float(trade["capital"])
    if event["reason"] == "take_profit_partial_30m":
        lot.take_profit_done = True
        remaining_bars30 = bars30[pd.to_datetime(bars30["datetime"]) > event["dt"]].copy()
        remaining_semantic = semantic_bars[pd.to_datetime(semantic_bars["datetime"]) > event["dt"]].copy()
        return _process_lot_exits(
            lot=lot,
            bars30=remaining_bars30,
            semantic_bars=remaining_semantic,
            cash=cash,
            trades=trades,
            prev_low=prev_low,
            fill_profile=fill_profile,
            gap_confirm_bars=gap_confirm_bars,
            semantic_mode=semantic_mode,
            trailing_after_tp=trailing_after_tp,
        )
    return None, cash


def _run_extension(signal_source: Path, output_dir: Path, profile: ExtensionProfile, start_date: str, end_date: str) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    signals = _load_signals(signal_source, start_date, end_date)
    trade_dates = _load_trade_dates(start_date, end_date)
    if not trade_dates:
        raise RuntimeError("No trade dates available for backtest.")
    date_idx = {date: i for i, date in enumerate(trade_dates)}
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
    bars30 = _load_minute_bars(codes, start_date, end_date, 30)
    bars30_by_code_date = {
        (code, date): g.sort_values("datetime").copy()
        for (code, date), g in bars30.groupby(["code", "bar_date"], sort=False)
    }
    fill_profile = FillProfile("gap_confirm_30m_close__intraday_30m_close", gap_open_mode="confirm_30m", intraday_mode="bar_close")

    cash = 150000.0
    lots: list[Lot] = []
    trades: list[dict[str, Any]] = []
    curve: list[dict[str, Any]] = []

    for date in trade_dates:
        next_lots: list[Lot] = []
        for lot in lots:
            day_bars30 = bars30_by_code_date.get((lot.code, date), pd.DataFrame())
            if not day_bars30.empty:
                day_bars30 = day_bars30[day_bars30["datetime"] > lot.buy_datetime].copy()
            semantic_mode = getattr(lot, "semantic_mode", "prev_low")
            trailing_after_tp = getattr(lot, "trailing_after_tp", None)
            lot_after, cash = _process_lot_exits(
                lot=lot,
                bars30=day_bars30,
                semantic_bars=day_bars30,
                cash=cash,
                trades=trades,
                prev_low=prev_low_map.get((lot.code, date)),
                fill_profile=fill_profile,
                gap_confirm_bars=day_bars30,
                semantic_mode=semantic_mode,
                trailing_after_tp=trailing_after_tp,
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
                target_capital = min(sizing_equity / 2.0, sizing_equity * 0.50)
                strong_today = _strong_mask(candidates)
                for is_strong, row in zip(strong_today.tolist(), candidates.itertuples(index=False)):
                    capital_weight = float(getattr(row, "capital_weight", 1.0))
                    if not np.isfinite(capital_weight):
                        capital_weight = 1.0
                    capital_each = min(cash, target_capital * max(0.0, min(capital_weight, 1.0)))
                    if capital_each <= 0:
                        break
                    buy_price = float(getattr(row, "entry_price")) * 1.0005
                    if buy_price <= 0 or not np.isfinite(buy_price):
                        continue
                    entry_idx = date_idx[date]
                    hold_days = profile.strong_hold_days if is_strong else profile.normal_hold_days
                    sell_idx = min(entry_idx + hold_days, len(trade_dates) - 1)
                    buy_fee = capital_each * 2.5 / 10000.0
                    shares = (capital_each - buy_fee) / buy_price
                    cash -= capital_each
                    lot = Lot(
                        code=str(getattr(row, "code")),
                        name=str(getattr(row, "name")),
                        buy_date=date,
                        buy_datetime=getattr(row, "confirm_datetime"),
                        sell_date=trade_dates[sell_idx],
                        buy_price=buy_price,
                        shares=shares,
                        capital=capital_each,
                        v4_rank=int(getattr(row, "v4_rank")),
                        v4_score=float(getattr(row, "v4_score")),
                        peak_price=buy_price,
                    )
                    lot.semantic_mode = profile.strong_semantic if is_strong else profile.normal_semantic
                    lot.trailing_after_tp = profile.strong_trailing_after_tp if is_strong else profile.normal_trailing_after_tp
                    lot.strong_signal = bool(is_strong)
                    lots.append(lot)

        market_value = 0.0
        for lot in lots:
            close = _safe_price(price_map, lot.code, date) or lot.buy_price
            market_value += lot.shares * close
        equity = cash + market_value
        curve.append({"date": date, "cash": cash, "market_value": market_value, "equity": equity, "holding_count": len(lots)})

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


def _profiles() -> list[ExtensionProfile]:
    return [
        ExtensionProfile("baseline_hold10_prevlow", "hold10, previous-day-low exit after half take-profit"),
        ExtensionProfile("hold20_prevlow", "hold20 for all signals, previous-day-low exit still active", strong_hold_days=20, normal_hold_days=20),
        ExtensionProfile(
            "strong_hold20_no_prevlow",
            "quality/rank50 signals hold20 and disable previous-day-low exit after half take-profit",
            strong_hold_days=20,
            normal_hold_days=10,
            strong_semantic="none",
            normal_semantic="prev_low",
        ),
        ExtensionProfile(
            "strong_hold30_no_prevlow",
            "quality/rank50 signals hold30 and disable previous-day-low exit after half take-profit",
            strong_hold_days=30,
            normal_hold_days=10,
            strong_semantic="none",
            normal_semantic="prev_low",
        ),
        ExtensionProfile(
            "strong_hold20_trailing8",
            "quality/rank50 signals hold20, disable previous-day-low exit, use 8% profit trailing stop after half take-profit",
            strong_hold_days=20,
            normal_hold_days=10,
            strong_semantic="none",
            normal_semantic="prev_low",
            strong_trailing_after_tp=0.08,
        ),
        ExtensionProfile(
            "strong_hold30_trailing8",
            "quality/rank50 signals hold30, disable previous-day-low exit, use 8% profit trailing stop after half take-profit",
            strong_hold_days=30,
            normal_hold_days=10,
            strong_semantic="none",
            normal_semantic="prev_low",
            strong_trailing_after_tp=0.08,
        ),
    ]


def _write_report(output_dir: Path, rows: list[dict[str, Any]]) -> None:
    lines = [
        "# G2 Pullback Profit Extension Sweep",
        "",
        "Entry, sizing, costs, 30m -5% stop and +10% half take-profit stay unchanged.",
        "Only the remaining half position's semantic exit and max holding window are changed.",
        "",
        "| profile | signals | trades | total | excess | max_dd | win | avg_trade | note |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in rows:
        lines.append(
            f"| {row['key']} | {int(row.get('signal_count') or 0)} | {int(row.get('trade_count') or 0)} | "
            f"{_pct(row.get('total_return'))} | {_pct(row.get('excess_return'))} | {_pct(row.get('max_drawdown'))} | "
            f"{_pct(row.get('win_rate'))} | {_pct(row.get('avg_trade_return'))} | {row.get('note', '')} |"
        )
    (output_dir / "findings.md").write_text("\n".join(lines), encoding="utf-8")


def run(labeled_signals: Path, output_dir: Path, start_date: str, end_date: str, profile_keys: set[str] | None = None) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    signal_source = _prepare_signal_source(labeled_signals, output_dir, start_date, end_date)
    profiles = _profiles()
    if profile_keys:
        missing = sorted(profile_keys - {p.key for p in profiles})
        if missing:
            raise ValueError(f"Unknown profiles: {missing}")
        profiles = [p for p in profiles if p.key in profile_keys]
    rows: list[dict[str, Any]] = []
    for profile in profiles:
        summary = _run_extension(signal_source, output_dir / profile.key, profile, start_date, end_date)
        summary["key"] = profile.key
        summary["note"] = profile.note
        rows.append(summary)

    summary_rows = []
    for row in rows:
        summary_rows.append(
            {
                "key": row["key"],
                "note": row["note"],
                "signal_count": row.get("signal_count"),
                "trade_count": row.get("trade_count"),
                "final_equity": row.get("final_equity"),
                "total_return": row.get("total_return"),
                "benchmark_return": row.get("benchmark_return"),
                "excess_return": row.get("excess_return"),
                "max_drawdown": row.get("max_drawdown"),
                "win_rate": row.get("win_rate"),
                "avg_trade_return": row.get("avg_trade_return"),
                "exit_reason_counts": json.dumps(row.get("exit_reason_counts", {}), ensure_ascii=False, sort_keys=True),
            }
        )
    summary_df = pd.DataFrame(summary_rows).sort_values("total_return", ascending=False)
    summary_df.to_csv(output_dir / "summary.csv", index=False, encoding="utf-8-sig")
    payload = {
        "schema_version": 1,
        "start_date": start_date,
        "end_date": end_date,
        "labeled_signals": str(labeled_signals),
        "rows": summary_df.replace({np.nan: None}).to_dict("records"),
        "outputs": {"summary": "summary.csv", "report": "findings.md"},
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    _write_report(output_dir, summary_df.to_dict("records"))
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Sweep profit extension rules for G2 pullback confirmation signals.")
    parser.add_argument("--labeled-signals", default=str(DEFAULT_LABELED_SIGNALS))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--start-date", default="2024-07-09")
    parser.add_argument("--end-date", default="2026-05-21")
    parser.add_argument("--profiles", default="", help="Comma-separated profile keys. Empty means all profiles.")
    args = parser.parse_args()
    keys = {item.strip() for item in str(args.profiles).split(",") if item.strip()} if str(args.profiles).strip() else None
    payload = run(Path(args.labeled_signals), Path(args.output_dir), str(args.start_date), str(args.end_date), profile_keys=keys)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()

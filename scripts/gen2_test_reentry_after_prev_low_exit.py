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
from scripts.gen2_backtest_open_v1_portfolio import _json_default, _load_trade_dates, _pct  # noqa: E402
from scripts.gen2_compare_prev_low_exit_fills import FillProfile, _load_minute_bars  # noqa: E402
from scripts.gen2_sweep_open_v1_semantic_exits import _load_daily_ohlc  # noqa: E402
from scripts.gen2_sweep_pullback_profit_extension import _process_lot_exits  # noqa: E402

DEFAULT_TRADES = REPO_ROOT / "reports" / "gen2_pullback_profit_extension_sweep" / "baseline_hold10_prevlow" / "trades.csv"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "reports" / "gen2_reentry_after_prev_low_exit"


@dataclass(frozen=True)
class ReentryProfile:
    key: str
    note: str
    window_days: int = 5
    trigger: str = "reclaim_ma10_vol"
    capital_ratio: float = 1.0
    hold_days: int = 10


def _load_exit_rows(path: Path, start_date: str, end_date: str) -> pd.DataFrame:
    trades = pd.read_csv(path)
    trades["sell_datetime"] = pd.to_datetime(trades["sell_datetime"], errors="coerce")
    trades["sell_date"] = pd.to_datetime(trades["sell_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    trades["buy_datetime"] = pd.to_datetime(trades["buy_datetime"], errors="coerce")
    trades["code"] = trades["code"].astype(str)
    for col in ["sell_price", "capital", "v4_rank", "v4_score"]:
        trades[col] = pd.to_numeric(trades[col], errors="coerce")
    exits = trades[
        trades["exit_reason"].astype(str).isin(["weak_prev_day_low_break_30m_close", "weak_prev_day_low_gap_confirm_30m_close"])
    ].copy()
    exits = exits[(exits["sell_date"] >= start_date) & (exits["sell_date"] <= end_date)].copy()
    return exits.dropna(subset=["code", "sell_datetime", "sell_date", "sell_price", "capital"]).reset_index(drop=True)


def _add_bar_features(bars: pd.DataFrame) -> pd.DataFrame:
    if bars.empty:
        return bars
    d = bars.sort_values(["code", "datetime"]).copy()
    g = d.groupby("code", sort=False)
    d["ma10"] = g["close"].transform(lambda s: s.rolling(10, min_periods=10).mean())
    d["prev_ma10"] = g["ma10"].shift(1)
    d["prev_close"] = g["close"].shift(1)
    d["volume_ma5_prev"] = g["volume"].transform(lambda s: s.shift(1).rolling(5, min_periods=5).mean())
    return d


def _pick_reentry(exit_row: pd.Series, bars: pd.DataFrame, trade_dates: list[str], profile: ReentryProfile) -> Optional[pd.Series]:
    code = str(exit_row["code"])
    sell_dt = pd.Timestamp(exit_row["sell_datetime"])
    sell_date = str(exit_row["sell_date"])
    if sell_date not in trade_dates:
        return None
    end_idx = min(trade_dates.index(sell_date) + int(profile.window_days), len(trade_dates) - 1)
    end_date = trade_dates[end_idx]
    scan = bars[
        (bars["code"] == code)
        & (bars["datetime"] > sell_dt)
        & (bars["bar_date"] <= end_date)
    ].copy()
    if scan.empty:
        return None

    sell_day = bars[(bars["code"] == code) & (bars["bar_date"] == sell_date)].copy()
    exit_day_high = float(sell_day["high"].max()) if not sell_day.empty else np.nan
    sell_price = float(exit_row["sell_price"])

    vol_ok = pd.to_numeric(scan["volume"], errors="coerce") > pd.to_numeric(scan["volume_ma5_prev"], errors="coerce")
    if profile.trigger == "reclaim_ma10_vol":
        mask = (scan["close"] > scan["ma10"]) & (scan["prev_close"] <= scan["prev_ma10"]) & vol_ok
    elif profile.trigger == "close_above_sell_price_vol":
        mask = (scan["close"] > sell_price) & vol_ok
    elif profile.trigger == "break_exit_day_high_vol":
        mask = (scan["close"] > exit_day_high) & vol_ok
    else:
        raise ValueError(f"Unknown trigger: {profile.trigger}")
    hit = scan[mask.fillna(False)].copy()
    if hit.empty:
        return None
    return hit.iloc[0]


def _simulate_reentry_trade(
    exit_row: pd.Series,
    entry_bar: pd.Series,
    bars_by_code_date: dict[tuple[str, str], pd.DataFrame],
    prev_low_map: dict[tuple[str, str], float],
    trade_dates: list[str],
    profile: ReentryProfile,
) -> list[dict[str, Any]]:
    code = str(exit_row["code"])
    entry_dt = pd.Timestamp(entry_bar["datetime"])
    entry_date = entry_dt.strftime("%Y-%m-%d")
    if entry_date not in trade_dates:
        return []
    entry_idx = trade_dates.index(entry_date)
    sell_idx = min(entry_idx + int(profile.hold_days), len(trade_dates) - 1)
    capital = float(exit_row["capital"]) * float(profile.capital_ratio)
    buy_price = float(entry_bar["close"]) * 1.0005
    if capital <= 0 or buy_price <= 0 or not np.isfinite(buy_price):
        return []
    buy_fee = capital * 2.5 / 10000.0
    lot = Lot(
        code=code,
        name=str(exit_row["name"]),
        buy_date=entry_date,
        buy_datetime=entry_dt,
        sell_date=trade_dates[sell_idx],
        buy_price=buy_price,
        shares=(capital - buy_fee) / buy_price,
        capital=capital,
        v4_rank=int(float(exit_row["v4_rank"])),
        v4_score=float(exit_row["v4_score"]),
        peak_price=buy_price,
    )

    trades: list[dict[str, Any]] = []
    cash = 0.0
    fill_profile = FillProfile("gap_confirm_30m_close__intraday_30m_close", gap_open_mode="confirm_30m", intraday_mode="bar_close")
    active: Optional[Lot] = lot
    for date in trade_dates[entry_idx: sell_idx + 1]:
        if active is None:
            break
        day_bars = bars_by_code_date.get((code, date), pd.DataFrame())
        if not day_bars.empty:
            day_bars = day_bars[day_bars["datetime"] > active.buy_datetime].copy()
        active, cash = _process_lot_exits(
            lot=active,
            bars30=day_bars,
            semantic_bars=day_bars,
            cash=cash,
            trades=trades,
            prev_low=prev_low_map.get((code, date)),
            fill_profile=fill_profile,
            gap_confirm_bars=day_bars,
            semantic_mode="prev_low",
            trailing_after_tp=None,
        )
        if active is None:
            break
        if date >= active.sell_date:
            day = bars_by_code_date.get((code, date), pd.DataFrame())
            if day.empty:
                break
            close = float(day.iloc[-1]["close"])
            trade = _sell_trade(
                lot=active,
                sell_datetime=pd.Timestamp(f"{date} 15:00:00"),
                raw_price=close,
                ratio=1.0,
                reason="reentry_time_exit",
                sell_slippage_bps=5.0,
                commission_bps=2.5,
                stamp_tax_bps=5.0,
            )
            trades.append({k: v for k, v in trade.items() if k != "proceeds"})
            active = None
    for t in trades:
        t["source_exit_reason"] = str(exit_row["exit_reason"])
        t["source_sell_datetime"] = pd.Timestamp(exit_row["sell_datetime"]).strftime("%Y-%m-%d %H:%M:%S")
        t["reentry_trigger"] = profile.trigger
        t["reentry_profile"] = profile.key
    return trades


def _profiles() -> list[ReentryProfile]:
    return [
        ReentryProfile("ma10_w3_half", "Within 3 trading days, 30m reclaim MA10 with volume, half of exited remaining capital", window_days=3, trigger="reclaim_ma10_vol", capital_ratio=0.5),
        ReentryProfile("ma10_w5_half", "Within 5 trading days, 30m reclaim MA10 with volume, half of exited remaining capital", window_days=5, trigger="reclaim_ma10_vol", capital_ratio=0.5),
        ReentryProfile("sell_price_w5_half", "Within 5 trading days, 30m close above exit sell price with volume, half size", window_days=5, trigger="close_above_sell_price_vol", capital_ratio=0.5),
        ReentryProfile("exit_high_w5_half", "Within 5 trading days, 30m close above exit-day high with volume, half size", window_days=5, trigger="break_exit_day_high_vol", capital_ratio=0.5),
        ReentryProfile("ma10_w5_full", "Within 5 trading days, 30m reclaim MA10 with volume, full exited remaining capital", window_days=5, trigger="reclaim_ma10_vol", capital_ratio=1.0),
    ]


def _summarize(trades: pd.DataFrame) -> dict[str, Any]:
    if trades.empty:
        return {
            "reentry_trade_rows": 0,
            "round_trip_count": 0,
            "total_pnl": 0.0,
            "total_capital": 0.0,
            "capital_return": None,
            "win_rate": None,
            "avg_trade_return": None,
            "exit_reason_counts": {},
        }
    agg = trades.groupby(["code", "buy_datetime"], as_index=False).agg(pnl=("pnl", "sum"), capital=("capital", "sum"))
    agg["round_return"] = agg["pnl"] / agg["capital"].replace(0, np.nan)
    return {
        "reentry_trade_rows": int(len(trades)),
        "round_trip_count": int(len(agg)),
        "total_pnl": float(trades["pnl"].sum()),
        "total_capital": float(agg["capital"].sum()),
        "capital_return": float(trades["pnl"].sum() / agg["capital"].sum()) if float(agg["capital"].sum()) > 0 else None,
        "win_rate": float((agg["round_return"] > 0).mean()) if not agg.empty else None,
        "avg_trade_return": float(agg["round_return"].mean()) if not agg.empty else None,
        "exit_reason_counts": trades["exit_reason"].astype(str).value_counts().to_dict(),
    }


def _write_report(output_dir: Path, rows: list[dict[str, Any]]) -> None:
    lines = [
        "# G2 Reentry After Previous-Low Exit",
        "",
        "Standalone reentry test after profitable lots are washed out by previous-day-low exits.",
        "Each reentry uses the exited remaining capital as sizing base; main portfolio slot conflicts are not modeled yet.",
        "",
        "| profile | candidates | trades | pnl | capital_ret | win | avg_trade | note |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in rows:
        lines.append(
            f"| {row['key']} | {int(row.get('candidate_count') or 0)} | {int(row.get('round_trip_count') or 0)} | "
            f"{float(row.get('total_pnl') or 0):.2f} | {_pct(row.get('capital_return'))} | {_pct(row.get('win_rate'))} | "
            f"{_pct(row.get('avg_trade_return'))} | {row.get('note', '')} |"
        )
    (output_dir / "findings.md").write_text("\n".join(lines), encoding="utf-8")


def run(trades_path: Path, output_dir: Path, start_date: str, end_date: str, profile_keys: set[str] | None = None) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    exits = _load_exit_rows(trades_path, start_date, end_date)
    codes = exits["code"].dropna().astype(str).unique().tolist()
    trade_dates = _load_trade_dates(start_date, end_date)
    if not trade_dates:
        raise RuntimeError("No trade dates available.")
    bars = _add_bar_features(_load_minute_bars(codes, start_date, end_date, 30))
    bars_by_code_date = {
        (code, date): g.sort_values("datetime").copy()
        for (code, date), g in bars.groupby(["code", "bar_date"], sort=False)
    }
    daily = _load_daily_ohlc(codes, start_date, end_date)
    prev_low_map = {
        (row.code, row.trade_date): float(row.prev_low)
        for row in daily.itertuples(index=False)
        if row.prev_low is not None and np.isfinite(row.prev_low)
    }

    profiles = _profiles()
    if profile_keys:
        missing = sorted(profile_keys - {p.key for p in profiles})
        if missing:
            raise ValueError(f"Unknown profiles: {missing}")
        profiles = [p for p in profiles if p.key in profile_keys]

    rows: list[dict[str, Any]] = []
    for profile in profiles:
        candidates: list[dict[str, Any]] = []
        trades: list[dict[str, Any]] = []
        for _, exit_row in exits.iterrows():
            entry = _pick_reentry(exit_row, bars, trade_dates, profile)
            if entry is None:
                continue
            candidates.append(
                {
                    "code": str(exit_row["code"]),
                    "name": str(exit_row["name"]),
                    "source_sell_datetime": pd.Timestamp(exit_row["sell_datetime"]).strftime("%Y-%m-%d %H:%M:%S"),
                    "entry_datetime": pd.Timestamp(entry["datetime"]).strftime("%Y-%m-%d %H:%M:%S"),
                    "entry_price": float(entry["close"]),
                    "profile": profile.key,
                }
            )
            trades.extend(_simulate_reentry_trade(exit_row, entry, bars_by_code_date, prev_low_map, trade_dates, profile))
        profile_dir = output_dir / profile.key
        profile_dir.mkdir(parents=True, exist_ok=True)
        candidates_df = pd.DataFrame(candidates)
        trades_df = pd.DataFrame(trades)
        candidates_df.to_csv(profile_dir / "candidates.csv", index=False, encoding="utf-8-sig")
        trades_df.to_csv(profile_dir / "trades.csv", index=False, encoding="utf-8-sig")
        summary = _summarize(trades_df)
        summary.update({"key": profile.key, "note": profile.note, "candidate_count": int(len(candidates_df))})
        (profile_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
        rows.append(summary)

    summary_df = pd.DataFrame(rows).sort_values("total_pnl", ascending=False)
    summary_df.to_csv(output_dir / "summary.csv", index=False, encoding="utf-8-sig")
    payload = {
        "schema_version": 1,
        "start_date": start_date,
        "end_date": end_date,
        "source_trades": str(trades_path),
        "source_exit_count": int(len(exits)),
        "rows": summary_df.replace({np.nan: None}).to_dict("records"),
        "outputs": {"summary": "summary.csv", "report": "findings.md"},
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    _write_report(output_dir, summary_df.to_dict("records"))
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Test standalone reentry after previous-day-low exits.")
    parser.add_argument("--trades", default=str(DEFAULT_TRADES))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--start-date", default="2024-07-09")
    parser.add_argument("--end-date", default="2026-05-21")
    parser.add_argument("--profiles", default="", help="Comma-separated profile keys. Empty means all profiles.")
    args = parser.parse_args()
    keys = {item.strip() for item in str(args.profiles).split(",") if item.strip()} if str(args.profiles).strip() else None
    payload = run(Path(args.trades), Path(args.output_dir), str(args.start_date), str(args.end_date), profile_keys=keys)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()

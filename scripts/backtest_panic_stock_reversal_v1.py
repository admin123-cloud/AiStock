"""Backtest a stock-only panic gap reversal candidate without sector membership data."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backtest_sector_first_pullback_panic_v1 import _load_market_breadth
from utils.market_warehouse import clickhouse_query_df
from utils.paths import report_path


def _sql_dates(values: pd.Series) -> str:
    return ",".join(f"toDate('{pd.Timestamp(v):%Y-%m-%d}')" for v in values)


def _load_candidates(start: str, end: str, down_ratio: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    breadth = _load_market_breadth(start, end)
    breadth["trade_date"] = pd.to_datetime(breadth["trade_date"])
    signal_dates = breadth.loc[breadth["down_ratio"] >= down_ratio, "trade_date"]
    if signal_dates.empty:
        return breadth, pd.DataFrame()
    dates = _sql_dates(signal_dates)
    sql = f"""
    WITH features AS (
      SELECT code, trade_date, close,
        lagInFrame(close, 15) OVER (
          PARTITION BY code ORDER BY trade_date
          ROWS BETWEEN 15 PRECEDING AND 15 PRECEDING
        ) AS close_15d,
        leadInFrame(open, 1) OVER (
          PARTITION BY code ORDER BY trade_date
          ROWS BETWEEN 1 FOLLOWING AND 1 FOLLOWING
        ) AS next_open,
        leadInFrame(close, 1) OVER (
          PARTITION BY code ORDER BY trade_date
          ROWS BETWEEN 1 FOLLOWING AND 1 FOLLOWING
        ) AS next_close
      FROM kline_daily
      WHERE code IN (SELECT code FROM stocks WHERE type = 'stock')
        AND trade_date BETWEEN toDate('{start}') AND toDate('{end}')
    )
    SELECT code, trade_date, close, close_15d, next_open, next_close
    FROM features
    WHERE trade_date IN ({dates})
    """
    d = clickhouse_query_df(sql)
    for col in ["close", "close_15d", "next_open", "next_close"]:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    d["trade_date"] = pd.to_datetime(d["trade_date"], errors="coerce")
    d = d.dropna(subset=["code", "trade_date", "close", "close_15d", "next_open", "next_close"])
    d["runup_15d"] = d["close"] / d["close_15d"] - 1.0
    d["gap"] = d["next_open"] / d["close"] - 1.0
    d["gross_return"] = d["next_close"] / d["next_open"] - 1.0
    return breadth, d[d["runup_15d"] >= 0.20].copy()


def _add_market_gate(candidates: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    if candidates.empty:
        return candidates
    warmup_start = (pd.Timestamp(start) - pd.Timedelta(days=120)).strftime("%Y-%m-%d")
    index = clickhouse_query_df(
        """
        SELECT trade_date, close
        FROM kline_daily
        WHERE code = '000300.SH' AND trade_date BETWEEN toDate(?) AND toDate(?)
        ORDER BY trade_date
        """,
        [warmup_start, end],
    )
    index["trade_date"] = pd.to_datetime(index["trade_date"], errors="coerce")
    index["close"] = pd.to_numeric(index["close"], errors="coerce")
    index = index.sort_values("trade_date")
    index["ma60"] = index["close"].rolling(60, min_periods=60).mean()
    index["above_ma60"] = index["close"] >= index["ma60"]
    return candidates.merge(index[["trade_date", "above_ma60"]], on="trade_date", how="left")


def _entry_audit(trades: pd.DataFrame, breadth: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        out = trades.copy()
        out["entry_date"] = pd.Series(dtype="datetime64[ns]")
        out["actual_entry_open"] = pd.Series(dtype="float64")
        out["volume"] = pd.Series(dtype="float64")
        out["amount"] = pd.Series(dtype="float64")
        out["turnover_ok"] = pd.Series(dtype="bool")
        out["limit_pct"] = pd.Series(dtype="float64")
        out["unfillable_limit_down"] = pd.Series(dtype="bool")
        return out
    calendar = sorted(pd.to_datetime(breadth["trade_date"]).dropna().unique())
    next_day = {calendar[i]: calendar[i + 1] for i in range(len(calendar) - 1)}
    trades = trades.copy()
    trades["entry_date"] = trades["trade_date"].map(next_day)
    pairs = ",".join(
        f"('{row.code}', toDate('{row.entry_date:%Y-%m-%d}'))"
        for row in trades.itertuples()
        if pd.notna(row.entry_date)
    )
    if not pairs:
        return trades.assign(turnover_ok=False, unfillable_limit_down=True)
    bars = clickhouse_query_df(
        f"""
        SELECT code, trade_date, open, volume, amount
        FROM kline_daily
        WHERE (code, trade_date) IN ({pairs})
        """
    )
    bars["trade_date"] = pd.to_datetime(bars["trade_date"], errors="coerce")
    for col in ["open", "volume", "amount"]:
        bars[col] = pd.to_numeric(bars[col], errors="coerce")
    bars = bars.rename(columns={"trade_date": "entry_date", "open": "actual_entry_open"})
    out = trades.merge(bars, on=["code", "entry_date"], how="left")
    out["turnover_ok"] = out["volume"].gt(0) & out["amount"].gt(0)
    out["limit_pct"] = np.where(out["code"].astype(str).str.startswith(("300", "688")), 0.195, 0.095)
    out["unfillable_limit_down"] = (
        out["actual_entry_open"] / out["close"] - 1.0 <= -out["limit_pct"] + 0.002
    )
    return out


def run(args: argparse.Namespace) -> dict:
    breadth, candidates = _load_candidates(args.start, args.end, args.down_ratio)
    candidates = _add_market_gate(candidates, args.start, args.end)
    candidates = candidates[
        candidates["above_ma60"].fillna(False)
        & candidates["gap"].between(args.gap_min, args.gap_max)
    ].copy()
    candidates = candidates.sort_values(["trade_date", "runup_15d"], ascending=[True, False])
    selected = candidates.groupby("trade_date", sort=True).head(args.top).copy()
    selected["net_return"] = selected["gross_return"] - args.cost_bps / 10000.0
    # Cooldown is based on a confirmed signal, even when execution later proves
    # impossible. This keeps the live shadow contract deterministic.
    signal_dates = selected.groupby("trade_date")["net_return"].mean().sort_index().index
    chosen_dates: list[pd.Timestamp] = []
    next_free: pd.Timestamp | None = None
    for date in signal_dates:
        if next_free is None or date >= next_free:
            chosen_dates.append(date)
            next_free = date + pd.Timedelta(days=args.cooldown + 1)
    selected = selected[selected["trade_date"].isin(chosen_dates)].copy()
    selected = _entry_audit(selected, breadth)
    selected = selected[
        selected["turnover_ok"].fillna(False) & ~selected["unfillable_limit_down"].fillna(True)
    ].copy()
    event_ret = selected.groupby("trade_date")["net_return"].mean().sort_index()
    equity = (1.0 + event_ret).cumprod()
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    selected.to_parquet(out / "selected_positions.parquet", index=False)
    pd.DataFrame({"trade_date": event_ret.index, "strategy_return": event_ret.values, "equity": equity.values}).to_csv(
        out / "equity_curve.csv", index=False
    )
    summary = {
        "execution_valid": False,
        "result_kind": "non_executable_intraday_price_proxy",
        "invalid_reason": "a_share_t_plus_1_same_day_exit_invalid",
        "research_only": True,
        "start": args.start,
        "end": args.end,
        "selected_positions": int(len(selected)),
        "active_events": int(len(event_ret)),
        "total_return": float(equity.iloc[-1] - 1.0) if len(equity) else 0.0,
        "max_drawdown": float((equity / equity.cummax() - 1.0).min()) if len(equity) else 0.0,
        "formal_buy_signal": False,
        "auto_order_allowed": False,
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default="2026-09-24")
    parser.add_argument("--down-ratio", type=float, default=0.60)
    parser.add_argument("--gap-min", type=float, default=-0.10)
    parser.add_argument("--gap-max", type=float, default=-0.05)
    parser.add_argument("--top", type=int, default=3)
    parser.add_argument("--cooldown", type=int, default=5)
    parser.add_argument("--cost-bps", type=float, default=50.0)
    parser.add_argument("--output-dir", default=str(report_path("panic_stock_reversal_shadow_v1")))
    print(json.dumps(run(parser.parse_args()), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

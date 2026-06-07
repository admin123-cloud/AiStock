from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts.backtest_v5_raw import DEFAULT_MAX_MEMORY_MB, _ensure_output_dir, _json_default
from scripts.backtest_v6_regime_daily import (
    DEFAULT_BUY_TIMING,
    REGIME_CONFIG,
    V6RegimeDailyBacktester,
    _silence_mysql_query_logs,
)


OUTPUT_ROOT = REPO_ROOT / "reports" / "v6_icepoint_hybrid"


class IcepointHybridBacktester(V6RegimeDailyBacktester):
    def __init__(
        self,
        *args: Any,
        range_monthly_n: int,
        range_entry_score: float,
        **kwargs: Any,
    ) -> None:
        self.range_monthly_n = int(range_monthly_n)
        self.range_entry_score = float(range_entry_score)
        self.range_icepoint_dates: Set[str] = set()
        super().__init__(*args, **kwargs)
        self.range_icepoint_dates = self._build_range_icepoint_dates()

    def _build_range_icepoint_dates(self) -> Set[str]:
        rows: List[Dict[str, Any]] = []
        for trade_date in self.trade_dates:
            regime, info = self._resolve_regime(trade_date)
            if regime != "range":
                continue
            breadth = info.get("breadth")
            if breadth is None:
                continue
            rows.append({"date": trade_date, "breadth": float(breadth)})
        if not rows:
            return set()
        df = pd.DataFrame(rows)
        df["month"] = pd.to_datetime(df["date"]).dt.to_period("M")
        picked = (
            df.sort_values(["month", "breadth", "date"])
            .groupby("month", group_keys=False)
            .head(self.range_monthly_n)
        )
        return set(picked["date"].astype(str).tolist())

    def _score_slice(self, trade_date: str, regime: str) -> pd.DataFrame:
        if regime == "range" and trade_date not in self.range_icepoint_dates:
            return pd.DataFrame()
        return super()._score_slice(trade_date, regime)


def _run_variant(
    name: str,
    start_date: str,
    end_date: str,
    output_dir: Path,
    range_monthly_n: int,
    range_hold_days: int,
    range_invest_ratio: float,
    range_entry_score: float,
    max_memory_mb: int,
    day_cache_limit: int,
) -> Dict[str, Any]:
    original_config = copy.deepcopy(REGIME_CONFIG)
    try:
        REGIME_CONFIG["range"].update(
            {
                "invest_ratio": float(range_invest_ratio),
                "max_holdings": 1,
                "entry_score": float(range_entry_score),
                "time_exit_days": int(range_hold_days),
                "trend_ma": "ma10",
                "min_hold_trend": max(1, min(2, int(range_hold_days))),
            }
        )
        backtester = IcepointHybridBacktester(
            start_date=start_date,
            end_date=end_date,
            initial_capital=100000.0,
            max_memory_mb=max_memory_mb,
            buy_timing=DEFAULT_BUY_TIMING,
            day_cache_limit=day_cache_limit,
            cost_profile="current",
            range_monthly_n=range_monthly_n,
            range_entry_score=range_entry_score,
        )
        backtester.strategy_version = f"v6_icepoint_hybrid_{name}"
        backtester.strategy_name = f"V6 Icepoint Hybrid {name}"
        result = backtester.run()
    finally:
        REGIME_CONFIG.clear()
        REGIME_CONFIG.update(original_config)

    variant_dir = output_dir / name
    _ensure_output_dir(variant_dir)
    summary = result["summary"]
    summary["variant"] = name
    summary["range_monthly_n"] = int(range_monthly_n)
    summary["range_hold_days"] = int(range_hold_days)
    summary["range_invest_ratio"] = float(range_invest_ratio)
    summary["range_entry_score"] = float(range_entry_score)
    summary["range_icepoint_days"] = len(backtester.range_icepoint_dates)
    (variant_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    result["curve_df"].to_csv(variant_dir / "equity_curve.csv", index=False, encoding="utf-8-sig")
    result["trades_df"].to_csv(variant_dir / "daily_trades.csv", index=False, encoding="utf-8-sig")
    result["decisions_df"].to_csv(variant_dir / "daily_decisions.csv", index=False, encoding="utf-8-sig")
    result["regime_df"].to_csv(variant_dir / "regime_trace.csv", index=False, encoding="utf-8-sig")
    return summary


def main() -> int:
    _silence_mysql_query_logs()
    parser = argparse.ArgumentParser(description="V6 trend-up plus range icepoint hybrid backtest.")
    parser.add_argument("--start-date", default="2021-04-24")
    parser.add_argument("--end-date", default="2026-04-24")
    parser.add_argument("--output-dir", default=str(OUTPUT_ROOT))
    parser.add_argument("--max-memory-mb", type=int, default=DEFAULT_MAX_MEMORY_MB)
    parser.add_argument("--day-cache-limit", type=int, default=32)
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    _ensure_output_dir(output_dir)

    variants = [
        ("n2_hold2", 2, 2, 0.30, 0.83),
        ("n2_hold3", 2, 3, 0.30, 0.83),
        ("n3_hold2", 3, 2, 0.30, 0.83),
        ("n3_hold3", 3, 3, 0.30, 0.83),
    ]
    summaries = []
    for name, monthly_n, hold_days, invest_ratio, entry_score in variants:
        print(f"running {name}...")
        summaries.append(
            _run_variant(
                name=name,
                start_date=args.start_date,
                end_date=args.end_date,
                output_dir=output_dir,
                range_monthly_n=monthly_n,
                range_hold_days=hold_days,
                range_invest_ratio=invest_ratio,
                range_entry_score=entry_score,
                max_memory_mb=int(args.max_memory_mb),
                day_cache_limit=int(args.day_cache_limit),
            )
        )

    compare = pd.DataFrame(
        [
            {
                "variant": item["variant"],
                "total_return": item["total_return"],
                "annual_return": item["annual_return"],
                "sharpe": item["sharpe"],
                "max_drawdown": item["max_drawdown"],
                "trade_count": item["trade_count"],
                "win_rate": item["win_rate"],
                "buy_regime_counts": json.dumps(item.get("buy_regime_counts", {}), ensure_ascii=False),
                "range_icepoint_days": item["range_icepoint_days"],
            }
            for item in summaries
        ]
    )
    compare.to_csv(output_dir / "summary_compare.csv", index=False, encoding="utf-8-sig")
    (output_dir / "summary_compare.json").write_text(
        json.dumps(summaries, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    print(compare.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

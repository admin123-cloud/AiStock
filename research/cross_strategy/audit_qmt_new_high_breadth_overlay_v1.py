"""Audit the QMT new-high breadth environment on an unchanged breakout trade ledger."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from utils.paths import report_path  # noqa: E402


DEFAULT_BREADTH = report_path("qmt_new_high_breadth_v1", "new_high_breadth_daily.parquet")
DEFAULT_TRADES = report_path(
    "g3_recalled_mainwave_contract_v1",
    "20200101_20260630_breakout_score88_sector2_stop10%_top10",
    "two_slot_closed_trades.csv",
)
DEFAULT_OUTPUT = report_path("qmt_new_high_breadth_overlay_audit_v1")


def _window(date: pd.Series) -> pd.Series:
    value = pd.to_datetime(date)
    return pd.Series(
        pd.cut(value, [pd.Timestamp("2019-12-31"), pd.Timestamp("2023-12-31"), pd.Timestamp("2025-12-31"), pd.Timestamp("2100-01-01")], labels=["2020-2023", "2024-2025", "2026YTD"]),
        index=date.index,
    ).astype(str)


def run(breadth_path: Path, trades_path: Path, output_dir: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    breadth = pd.read_parquet(breadth_path)
    breadth["trade_date"] = pd.to_datetime(breadth["trade_date"]).dt.strftime("%Y-%m-%d")
    trades = pd.read_csv(trades_path)
    trades["entry_date"] = pd.to_datetime(trades["entry_date"]).dt.strftime("%Y-%m-%d")
    keep = ["trade_date", "nh20_breadth", "nh100_breadth", "nhall_breadth", "breakout_environment"]
    joined = trades.merge(breadth[keep], left_on="entry_date", right_on="trade_date", how="left")
    joined["window"] = _window(joined["entry_date"])
    joined["breakout_environment"] = joined["breakout_environment"].fillna(False).astype(bool)
    summary = (
        joined.groupby(["window", "breakout_environment"], dropna=False)
        .agg(
            trades=("code", "size"),
            avg_net_return=("net_ret", "mean"),
            median_net_return=("net_ret", "median"),
            win_rate=("net_ret", lambda s: (s > 0).mean()),
            avg_hold_days=("hold_days", "mean"),
        )
        .reset_index()
    )
    all_summary = (
        joined.groupby("breakout_environment")
        .agg(trades=("code", "size"), avg_net_return=("net_ret", "mean"), win_rate=("net_ret", lambda s: (s > 0).mean()))
        .reset_index()
    )
    joined.to_csv(output_dir / "joined_breakout_trades.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(output_dir / "window_summary.csv", index=False, encoding="utf-8-sig")
    all_summary.to_csv(output_dir / "all_summary.csv", index=False, encoding="utf-8-sig")
    payload = {
        "research_only": True,
        "method": "Unchanged historical G3 breakout ledger grouped by the QMT new-high breadth environment observed on entry date. This is an overlay audit, not a re-simulated pre-breakout-entry strategy.",
        "trade_rows": int(len(joined)),
        "unmatched_breadth_dates": int(joined["nh100_breadth"].isna().sum()),
        "all_summary": all_summary.to_dict("records"),
        "window_summary": summary.to_dict("records"),
        "promotion": "no_promote_pending_prebreakout_resimulation",
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit QMT new-high breadth on an unchanged breakout ledger")
    parser.add_argument("--breadth", default=str(DEFAULT_BREADTH))
    parser.add_argument("--trades", default=str(DEFAULT_TRADES))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()
    print(json.dumps(run(Path(args.breadth), Path(args.trades), Path(args.output_dir)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

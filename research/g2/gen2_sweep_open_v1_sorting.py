from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

REPO_ROOT = _PROJECT_ROOT
sys.path.insert(0, str(REPO_ROOT))

from scripts.gen2_backtest_open_v1_intraday_risk import run  # noqa: E402
from scripts.gen2_backtest_open_v1_portfolio import DEFAULT_SIGNAL_SOURCE, SORT_MODES  # noqa: E402

DEFAULT_OUTPUT_DIR = _report_path() / "gen2_open_v1_sorting_sweep"


from research.common.reporting import numpy_json_default as _json_default


from research.common.reporting import percent_text as _pct


def run_sweep(output_dir: Path, signal_source: Path, start_date: str, end_date: str) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    profiles = [
        {"profile": "return_tp10", "stop_loss": None, "take_profit": 0.10, "trailing_stop": None},
        {"profile": "steady_tp10_sl6", "stop_loss": 0.06, "take_profit": 0.10, "trailing_stop": None},
    ]
    rows: list[dict[str, Any]] = []
    for profile in profiles:
        for sort_mode in sorted(SORT_MODES):
            name = f"{profile['profile']}_{sort_mode}"
            summary = run(
                signal_source=signal_source,
                output_dir=output_dir / "runs" / name,
                start_date=start_date,
                end_date=end_date,
                initial_cash=150000.0,
                max_positions=3,
                max_buys_per_day=3,
                hold_days=3,
                benchmark_code="000852.SH",
                buy_slippage_bps=5.0,
                sell_slippage_bps=5.0,
                commission_bps=2.5,
                stamp_tax_bps=5.0,
                stop_loss_pct=profile["stop_loss"],
                take_profit_pct=profile["take_profit"],
                take_profit_sell_ratio=0.5,
                trailing_stop_pct=profile["trailing_stop"],
                sort_mode=sort_mode,
                max_position_weight=0.30,
            )
            rows.append(
                {
                    "profile": profile["profile"],
                    "sort_mode": sort_mode,
                    "total_return": summary.get("total_return"),
                    "benchmark_return": summary.get("benchmark_return"),
                    "excess_return": summary.get("excess_return"),
                    "max_drawdown": summary.get("max_drawdown"),
                    "trade_count": summary.get("trade_count"),
                    "win_rate": summary.get("win_rate"),
                    "avg_trade_return": summary.get("avg_trade_return"),
                    "final_equity": summary.get("final_equity"),
                }
            )

    df = pd.DataFrame(rows)
    df["score"] = df["excess_return"].fillna(0.0) + df["max_drawdown"].fillna(0.0) * 0.75
    df = df.sort_values(["profile", "score"], ascending=[True, False])
    df.to_csv(output_dir / "summary.csv", index=False, encoding="utf-8-sig")
    payload = {
        "schema_version": 1,
        "start_date": start_date,
        "end_date": end_date,
        "rows": df.to_dict("records"),
        "outputs": {"summary": "summary.csv", "runs": "runs/"},
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    _write_findings(output_dir, df)
    return payload


def _write_findings(output_dir: Path, df: pd.DataFrame) -> None:
    lines = [
        "# G2 Open V1 Sorting Sweep",
        "",
        "| profile | sort | total | excess | max_dd | win | trades |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in df.to_dict("records"):
        lines.append(
            f"| {row['profile']} | {row['sort_mode']} | {_pct(row.get('total_return'))} | {_pct(row.get('excess_return'))} | {_pct(row.get('max_drawdown'))} | {_pct(row.get('win_rate'))} | {int(row.get('trade_count') or 0)} |"
        )
    (output_dir / "findings.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Sweep G2 Open V1 same-day candidate sorting modes.")
    parser.add_argument("--signal-source", default=str(DEFAULT_SIGNAL_SOURCE))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--start-date", default="2024-07-09")
    parser.add_argument("--end-date", default="2026-05-21")
    args = parser.parse_args()
    payload = run_sweep(Path(args.output_dir), Path(args.signal_source), str(args.start_date), str(args.end_date))
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()

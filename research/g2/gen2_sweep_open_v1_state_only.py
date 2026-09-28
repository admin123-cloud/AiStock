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

from api.gen2_strategy import GEN2_OPEN_RULE_V1  # noqa: E402
from scripts.gen2_backtest_open_v1_intraday_risk import run  # noqa: E402
from scripts.gen2_backtest_open_v1_portfolio import DEFAULT_SIGNAL_SOURCE  # noqa: E402

DEFAULT_OUTPUT_DIR = _report_path() / "gen2_open_v1_state_only_sweep"


from research.common.reporting import numpy_json_default as _json_default


from research.common.reporting import percent_text as _pct


def run_sweep(
    output_dir: Path,
    signal_source: Path,
    start_date: str,
    end_date: str,
    states: list[str],
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    original_state = str(GEN2_OPEN_RULE_V1.get("g2_open_state") or "NORMAL")
    rows: list[dict[str, Any]] = []
    try:
        for state in states:
            GEN2_OPEN_RULE_V1["g2_open_state"] = state
            summary = run(
                signal_source=signal_source,
                output_dir=output_dir / "runs" / state.lower(),
                start_date=start_date,
                end_date=end_date,
                initial_cash=150000.0,
                max_positions=2,
                max_buys_per_day=1,
                hold_days=3,
                benchmark_code="000852.SH",
                buy_slippage_bps=5.0,
                sell_slippage_bps=5.0,
                commission_bps=2.5,
                stamp_tax_bps=5.0,
                stop_loss_pct=0.05,
                take_profit_pct=0.10,
                take_profit_sell_ratio=0.5,
                trailing_stop_pct=None,
                sort_mode="trigger_time",
                max_position_weight=0.50,
            )
            rows.append(
                {
                    "state": state,
                    "signal_count": summary.get("signal_count"),
                    "trade_count": summary.get("trade_count"),
                    "total_return": summary.get("total_return"),
                    "benchmark_return": summary.get("benchmark_return"),
                    "excess_return": summary.get("excess_return"),
                    "max_drawdown": summary.get("max_drawdown"),
                    "win_rate": summary.get("win_rate"),
                    "avg_trade_return": summary.get("avg_trade_return"),
                    "final_equity": summary.get("final_equity"),
                }
            )
    finally:
        GEN2_OPEN_RULE_V1["g2_open_state"] = original_state

    df = pd.DataFrame(rows)
    df["attack_score"] = (
        df["excess_return"].fillna(0.0)
        + df["total_return"].fillna(0.0) * 0.25
        + df["max_drawdown"].fillna(0.0) * 0.75
    )
    df = df.sort_values(["attack_score", "total_return"], ascending=[False, False])
    df.to_csv(output_dir / "summary.csv", index=False, encoding="utf-8-sig")
    payload = {
        "schema_version": 1,
        "start_date": start_date,
        "end_date": end_date,
        "states": states,
        "fixed_rule": {
            "pattern": GEN2_OPEN_RULE_V1.get("pattern"),
            "trigger_type": GEN2_OPEN_RULE_V1.get("trigger_type"),
            "max_positions": 2,
            "max_buys_per_day": 1,
            "max_position_weight": 0.50,
            "sort_mode": "trigger_time",
            "stop_loss_pct": 0.05,
            "take_profit_pct": 0.10,
            "take_profit_sell_ratio": 0.5,
            "hold_days": 3,
        },
        "rows": df.where(pd.notna(df), None).to_dict("records"),
        "outputs": {"summary": "summary.csv", "findings": "findings.md", "runs": "runs/"},
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    _write_findings(output_dir, df)
    return payload


def _write_findings(output_dir: Path, df: pd.DataFrame) -> None:
    lines = [
        "# G2 Open V1 State-Only Sweep",
        "",
        "Only `g2_open_state` changes. Pattern, trigger, position sizing, TP/SL, holding days and sort mode are unchanged.",
        "",
        "| state | signals | trades | total | excess | max_dd | win | avg_trade |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in df.to_dict("records"):
        lines.append(
            f"| {row['state']} | {int(row.get('signal_count') or 0)} | {int(row.get('trade_count') or 0)} | {_pct(row.get('total_return'))} | {_pct(row.get('excess_return'))} | {_pct(row.get('max_drawdown'))} | {_pct(row.get('win_rate'))} | {_pct(row.get('avg_trade_return'))} |"
        )
    (output_dir / "findings.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Sweep G2 Open V1 by market state only.")
    parser.add_argument("--signal-source", default=str(DEFAULT_SIGNAL_SOURCE))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--start-date", default="2024-07-09")
    parser.add_argument("--end-date", default="2026-05-21")
    parser.add_argument("--states", default="OFF,PROBE,AGGRESSIVE")
    args = parser.parse_args()
    states = [item.strip().upper() for item in str(args.states).split(",") if item.strip()]
    payload = run_sweep(
        output_dir=Path(args.output_dir),
        signal_source=Path(args.signal_source),
        start_date=str(args.start_date),
        end_date=str(args.end_date),
        states=states,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()

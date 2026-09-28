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
from scripts.gen2_backtest_open_v1_portfolio import DEFAULT_SIGNAL_SOURCE  # noqa: E402

DEFAULT_OUTPUT_DIR = _report_path() / "gen2_open_v1_robustness"


from research.common.reporting import numpy_json_default as _json_default


from research.common.reporting import percent_text as _pct


def _split_profiles() -> list[dict[str, Any]]:
    return [
        {"profile": "train_20240709_20250331", "start": "2024-07-09", "end": "2025-03-31", "segment": "train"},
        {"profile": "validation_20250401_20251231", "start": "2025-04-01", "end": "2025-12-31", "segment": "validation"},
        {"profile": "blind_20260101_20260521", "start": "2026-01-01", "end": "2026-05-21", "segment": "blind"},
        {"profile": "full_cost_base", "start": "2024-07-09", "end": "2026-05-21", "segment": "full", "slip": 5.0, "comm": 2.5},
        {"profile": "full_cost_high", "start": "2024-07-09", "end": "2026-05-21", "segment": "full_cost_stress", "slip": 10.0, "comm": 5.0},
        {"profile": "full_cost_very_high", "start": "2024-07-09", "end": "2026-05-21", "segment": "full_cost_stress", "slip": 20.0, "comm": 5.0},
    ]


def run_validation(output_dir: Path, signal_source: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for item in _split_profiles():
        slip = float(item.get("slip", 5.0))
        comm = float(item.get("comm", 2.5))
        summary = run(
            signal_source=signal_source,
            output_dir=output_dir / "runs" / str(item["profile"]),
            start_date=str(item["start"]),
            end_date=str(item["end"]),
            initial_cash=150000.0,
            max_positions=2,
            max_buys_per_day=1,
            hold_days=3,
            benchmark_code="000852.SH",
            buy_slippage_bps=slip,
            sell_slippage_bps=slip,
            commission_bps=comm,
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
                "profile": item["profile"],
                "segment": item["segment"],
                "start_date": item["start"],
                "end_date": item["end"],
                "buy_sell_slippage_bps": slip,
                "commission_bps": comm,
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

    df = pd.DataFrame(rows)
    df.to_csv(output_dir / "summary.csv", index=False, encoding="utf-8-sig")
    payload = {
        "schema_version": 1,
        "rows": df.where(pd.notna(df), None).to_dict("records"),
        "outputs": {"summary": "summary.csv", "findings": "findings.md", "runs": "runs/"},
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    _write_findings(output_dir, df)
    return payload


def _write_findings(output_dir: Path, df: pd.DataFrame) -> None:
    lines = [
        "# G2 Open V1 SL5 Robustness Validation",
        "",
        "| profile | segment | window | slip | comm | signals | trades | total | excess | max_dd | win | avg_trade |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in df.to_dict("records"):
        lines.append(
            f"| {row['profile']} | {row['segment']} | {row['start_date']}~{row['end_date']} | {float(row['buy_sell_slippage_bps']):.1f} | {float(row['commission_bps']):.1f} | {int(row.get('signal_count') or 0)} | {int(row.get('trade_count') or 0)} | {_pct(row.get('total_return'))} | {_pct(row.get('excess_return'))} | {_pct(row.get('max_drawdown'))} | {_pct(row.get('win_rate'))} | {_pct(row.get('avg_trade_return'))} |"
        )
    (output_dir / "findings.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate G2 Open V1 SL5 robustness by time split and cost stress.")
    parser.add_argument("--signal-source", default=str(DEFAULT_SIGNAL_SOURCE))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    args = parser.parse_args()
    payload = run_validation(output_dir=Path(args.output_dir), signal_source=Path(args.signal_source))
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()

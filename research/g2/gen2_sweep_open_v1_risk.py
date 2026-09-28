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
from typing import Any, Iterable, Optional

import numpy as np
import pandas as pd

REPO_ROOT = _PROJECT_ROOT
sys.path.insert(0, str(REPO_ROOT))

from scripts.gen2_backtest_open_v1_portfolio import DEFAULT_SIGNAL_SOURCE, run  # noqa: E402

DEFAULT_OUTPUT_DIR = _report_path() / "gen2_open_v1_risk_sweep"


from research.common.reporting import numpy_json_default as _json_default


def _parse_grid(text: str) -> list[Optional[float]]:
    values: list[Optional[float]] = []
    for item in str(text or "").split(","):
        token = item.strip().lower()
        if not token:
            continue
        if token in {"none", "null", "na"}:
            values.append(None)
        else:
            values.append(float(token))
    return values or [None]


def _combo_name(stop_loss: Optional[float], take_profit: Optional[float], trailing_stop: Optional[float]) -> str:
    def fmt(prefix: str, value: Optional[float]) -> str:
        return f"{prefix}none" if value is None else f"{prefix}{int(round(value * 100))}"

    return "_".join([fmt("sl", stop_loss), fmt("tp", take_profit), fmt("tr", trailing_stop)])


def run_sweep(
    output_dir: Path,
    signal_source: Path,
    start_date: str,
    end_date: str,
    stop_losses: Iterable[Optional[float]],
    take_profits: Iterable[Optional[float]],
    trailing_stops: Iterable[Optional[float]],
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for stop_loss in stop_losses:
        for take_profit in take_profits:
            for trailing_stop in trailing_stops:
                if trailing_stop is not None and take_profit is None:
                    continue
                name = _combo_name(stop_loss, take_profit, trailing_stop)
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
                    stop_loss_pct=stop_loss,
                    take_profit_pct=take_profit,
                    take_profit_sell_ratio=0.5,
                    trailing_stop_pct=trailing_stop,
                )
                rows.append(
                    {
                        "name": name,
                        "stop_loss_pct": stop_loss,
                        "take_profit_pct": take_profit,
                        "trailing_stop_pct": trailing_stop,
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

    summary_df = pd.DataFrame(rows)
    summary_df["score"] = (
        summary_df["excess_return"].fillna(0.0)
        + summary_df["total_return"].fillna(0.0) * 0.25
        + summary_df["max_drawdown"].fillna(0.0) * 0.75
    )
    summary_df = summary_df.sort_values(["score", "excess_return", "max_drawdown"], ascending=[False, False, False])
    summary_df.to_csv(output_dir / "summary.csv", index=False, encoding="utf-8-sig")
    payload = {
        "schema_version": 1,
        "start_date": start_date,
        "end_date": end_date,
        "combo_count": int(len(summary_df)),
        "best": summary_df.head(10).to_dict("records"),
        "outputs": {"summary": "summary.csv", "runs": "runs/"},
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    _write_findings(output_dir, summary_df)
    return payload


from research.common.reporting import percent_text as _pct


def _write_findings(output_dir: Path, summary_df: pd.DataFrame) -> None:
    lines = [
        "# G2 Open V1 Risk Sweep",
        "",
        "## Top Combos",
        "",
        "| name | total | excess | max_dd | win | trades |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in summary_df.head(12).to_dict("records"):
        lines.append(
            f"| {row['name']} | {_pct(row.get('total_return'))} | {_pct(row.get('excess_return'))} | {_pct(row.get('max_drawdown'))} | {_pct(row.get('win_rate'))} | {int(row.get('trade_count') or 0)} |"
        )
    (output_dir / "findings.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Sweep risk parameters for G2 Open V1 portfolio.")
    parser.add_argument("--signal-source", default=str(DEFAULT_SIGNAL_SOURCE))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--start-date", default="2024-07-09")
    parser.add_argument("--end-date", default="2026-05-21")
    parser.add_argument("--stop-losses", default="none,0.06,0.08,0.10")
    parser.add_argument("--take-profits", default="none,0.08,0.10,0.12")
    parser.add_argument("--trailing-stops", default="none,0.06,0.08")
    args = parser.parse_args()
    payload = run_sweep(
        output_dir=Path(args.output_dir),
        signal_source=Path(args.signal_source),
        start_date=str(args.start_date),
        end_date=str(args.end_date),
        stop_losses=_parse_grid(args.stop_losses),
        take_profits=_parse_grid(args.take_profits),
        trailing_stops=_parse_grid(args.trailing_stops),
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()

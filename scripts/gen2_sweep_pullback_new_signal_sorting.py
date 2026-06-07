from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts.gen2_backtest_open_v1_portfolio import SORT_MODES, _json_default, _pct  # noqa: E402
from scripts.gen2_compare_prev_low_exit_fills import FillProfile, _run_profile  # noqa: E402

DEFAULT_LABELED_SIGNALS = REPO_ROOT / "reports" / "gen2_v4_factor_profile" / "pullback_signal_factor_labeled.parquet"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "reports" / "gen2_pullback_new_signal_sorting"


def _write_findings(output_dir: Path, rows: list[dict[str, Any]]) -> None:
    lines = [
        "# G2 Pullback New Signal Sorting Sweep",
        "",
        "Only same-day new-signal ordering changes. Signal source, realistic entry fill, max 2 positions, max 1 buy/day, +10% half take-profit, -5% 30m stop and weak previous-low exit remain unchanged.",
        "",
        "| sort | signals | trades | total | benchmark | excess | max_dd | win | avg_trade | exits |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in rows:
        lines.append(
            f"| {row['sort_mode']} | {int(row.get('signal_count') or 0)} | {int(row.get('trade_count') or 0)} | "
            f"{_pct(row.get('total_return'))} | {_pct(row.get('benchmark_return'))} | {_pct(row.get('excess_return'))} | "
            f"{_pct(row.get('max_drawdown'))} | {_pct(row.get('win_rate'))} | {_pct(row.get('avg_trade_return'))} | "
            f"{row.get('exit_reason_counts') or ''} |"
        )
    (output_dir / "findings.md").write_text("\n".join(lines), encoding="utf-8")


def run(
    signal_source: Path,
    output_dir: Path,
    start_date: str,
    end_date: str,
    sort_modes: list[str] | None = None,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    modes = sort_modes or sorted(SORT_MODES)
    profile = FillProfile(
        "gap_confirm_30m_close__intraday_30m_close",
        gap_open_mode="confirm_30m",
        intraday_mode="bar_close",
    )

    rows: list[dict[str, Any]] = []
    for sort_mode in modes:
        summary = _run_profile(
            signal_source=signal_source,
            output_dir=output_dir / "runs" / sort_mode,
            profile=profile,
            start_date=start_date,
            end_date=end_date,
            sort_mode=sort_mode,
        )
        rows.append(
            {
                "sort_mode": sort_mode,
                "signal_count": summary.get("signal_count"),
                "trade_count": summary.get("trade_count"),
                "total_return": summary.get("total_return"),
                "benchmark_return": summary.get("benchmark_return"),
                "excess_return": summary.get("excess_return"),
                "max_drawdown": summary.get("max_drawdown"),
                "win_rate": summary.get("win_rate"),
                "avg_trade_return": summary.get("avg_trade_return"),
                "final_equity": summary.get("final_equity"),
                "exit_reason_counts": json.dumps(summary.get("exit_reason_counts", {}), ensure_ascii=False, sort_keys=True),
            }
        )

    df = pd.DataFrame(rows)
    df["score"] = df["total_return"].fillna(0.0) + df["max_drawdown"].fillna(0.0) * 0.50
    df = df.sort_values(["score", "total_return"], ascending=[False, False]).reset_index(drop=True)
    df.to_csv(output_dir / "summary.csv", index=False, encoding="utf-8-sig")
    ordered_rows = df.where(pd.notna(df), None).to_dict("records")
    _write_findings(output_dir, ordered_rows)

    payload = {
        "schema_version": 1,
        "start_date": start_date,
        "end_date": end_date,
        "signal_source": str(signal_source),
        "fill_profile": profile.__dict__,
        "rows": ordered_rows,
        "outputs": {"summary": "summary.csv", "findings": "findings.md", "runs": "runs/"},
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Sweep G2 pullback same-day new-signal sorting with realistic exits.")
    parser.add_argument("--signal-source", default=str(DEFAULT_LABELED_SIGNALS))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--start-date", default="2024-07-09")
    parser.add_argument("--end-date", default="2026-05-21")
    parser.add_argument("--sort-modes", default=",".join(sorted(SORT_MODES)))
    args = parser.parse_args()
    sort_modes = [x.strip() for x in str(args.sort_modes).split(",") if x.strip()]
    payload = run(Path(args.signal_source), Path(args.output_dir), str(args.start_date), str(args.end_date), sort_modes)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()

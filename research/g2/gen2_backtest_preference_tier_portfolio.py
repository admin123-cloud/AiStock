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
from typing import Any, Callable

import pandas as pd

REPO_ROOT = _PROJECT_ROOT
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.gen2_backtest_open_v1_portfolio import _json_default, _pct  # noqa: E402
from scripts.gen2_compare_prev_low_exit_fills import FillProfile, _run_profile  # noqa: E402


DEFAULT_TIER_SIGNALS = _report_path() / "gen2_preference_signal_tier_validation" / "signals_with_preference_tiers.csv"
DEFAULT_OUTPUT_DIR = _report_path() / "gen2_preference_tier_portfolio_backtest"


def _load_tier_signals(path: Path) -> pd.DataFrame:
    d = pd.read_csv(path)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    d["confirm_datetime"] = pd.to_datetime(d["confirm_datetime"], errors="coerce")
    return d.dropna(subset=["entry_date", "code", "entry_price", "confirm_datetime"]).copy()


def _rule_masks(signals: pd.DataFrame) -> dict[str, tuple[str, Callable[[pd.DataFrame], pd.Series]]]:
    return {
        "baseline_all": ("All G2 signals, original portfolio logic.", lambda d: pd.Series(True, index=d.index)),
        "drop_c_risk": ("Drop only C-risk; keep A/B/C-aggressive/C-review.", lambda d: d["c_subtier"].ne("C-risk")),
        "drop_c_risk_review": ("Drop C-risk and C-review; keep A/B/C-aggressive.", lambda d: ~d["c_subtier"].isin(["C-risk", "C-review"])),
        "a_b_only": ("Keep A and B only; reject all C subtiers.", lambda d: d["preference_tier"].isin(["A", "B"])),
        "a_only": ("Keep A only.", lambda d: d["preference_tier"].eq("A")),
        "a_plus_c_aggressive": ("Keep A plus C-aggressive.", lambda d: d["preference_tier"].eq("A") | d["c_subtier"].eq("C-aggressive")),
        "c_aggressive_only": ("Keep C-aggressive only.", lambda d: d["c_subtier"].eq("C-aggressive")),
        "c_risk_only": ("Diagnostic only: C-risk signals.", lambda d: d["c_subtier"].eq("C-risk")),
    }


def _write_report(output_dir: Path, rows: list[dict[str, Any]]) -> None:
    lines = [
        "# G2 Preference Tier Portfolio Backtest",
        "",
        "Entry, position sizing, 30m stop loss, +10% half take-profit and previous-day-low exit logic are unchanged.",
        "Only the candidate signal set changes according to preference tier labels.",
        "",
        "| rule | filtered | used | trades | total | excess | max_dd | win | avg_trade | note |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in rows:
        lines.append(
            f"| {row['key']} | {int(row.get('filtered_signal_rows') or 0)} | {int(row.get('signal_count') or 0)} | "
            f"{int(row.get('trade_count') or 0)} | {_pct(row.get('total_return'))} | {_pct(row.get('excess_return'))} | "
            f"{_pct(row.get('max_drawdown'))} | {_pct(row.get('win_rate'))} | {_pct(row.get('avg_trade_return'))} | "
            f"{row.get('rule_note', '')} |"
        )
    (output_dir / "conclusion_zh.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    signals = _load_tier_signals(Path(args.tier_signals))
    profile = FillProfile("gap_confirm_30m_close__intraday_30m_close", gap_open_mode="confirm_30m", intraday_mode="bar_close")

    selected = _rule_masks(signals)
    if args.rules:
        wanted = {x.strip() for x in str(args.rules).split(",") if x.strip()}
        missing = wanted - set(selected)
        if missing:
            raise ValueError(f"Unknown rules: {sorted(missing)}")
        selected = {key: selected[key] for key in selected if key in wanted}

    rows: list[dict[str, Any]] = []
    for key, (note, mask_fn) in selected.items():
        rule_dir = output_dir / key
        rule_dir.mkdir(parents=True, exist_ok=True)
        filtered = signals[mask_fn(signals).fillna(False)].copy()
        signal_path = rule_dir / "signals.parquet"
        filtered.to_parquet(signal_path, index=False)
        filtered.to_csv(rule_dir / "signals.csv", index=False, encoding="utf-8-sig")
        summary = _run_profile(
            signal_source=signal_path,
            output_dir=rule_dir / "backtest",
            profile=profile,
            start_date=str(args.start_date),
            end_date=str(args.end_date),
            sort_mode=str(args.sort_mode),
        )
        summary["key"] = key
        summary["rule_note"] = note
        summary["filtered_signal_rows"] = int(len(filtered))
        rows.append(summary)

    summary_df = pd.DataFrame(rows)
    summary_df.to_csv(output_dir / "summary.csv", index=False, encoding="utf-8-sig")
    _write_report(output_dir, rows)
    payload = {
        "schema_version": 1,
        "tier_signals": str(Path(args.tier_signals)),
        "start_date": str(args.start_date),
        "end_date": str(args.end_date),
        "sort_mode": str(args.sort_mode),
        "rows": summary_df.where(pd.notna(summary_df), None).to_dict("records"),
        "outputs": {"summary": "summary.csv", "report": "conclusion_zh.md", "runs": "*/backtest/"},
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Portfolio backtest for G2 preference tier signal filters.")
    parser.add_argument("--tier-signals", default=str(DEFAULT_TIER_SIGNALS))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--start-date", default="2024-07-09")
    parser.add_argument("--end-date", default="2026-05-21")
    parser.add_argument("--sort-mode", default="trigger_time")
    parser.add_argument("--rules", default="")
    print(json.dumps(run(parser.parse_args()), ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()

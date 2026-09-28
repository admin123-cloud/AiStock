from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen2_backtest_open_v1_portfolio import _json_default, _pct  # noqa: E402
from scripts.gen2_candidate_outcome_supervision import _prepare_source  # noqa: E402
from scripts.gen2_compare_prev_low_exit_fills import FillProfile, _run_profile  # noqa: E402


DEFAULT_CANDIDATES = ROOT / "reports" / "gen2_candidate_outcome_supervision" / "labeled_candidates.parquet"
DEFAULT_OUTPUT_DIR = ROOT / "reports" / "gen2_risk_cool_param_sweep"


BASE = {
    "mom20_max": 0.30,
    "mom5_max": 0.14,
    "vol_ratio_max": 1.90,
    "rt_return_min": 0.00,
    "rt_return_max": 0.12,
    "amount_min": 1.50,
    "amount_max": 7.00,
}


def _variant_grid() -> list[dict[str, Any]]:
    variants: list[dict[str, Any]] = [{"variant": "base", **BASE}]
    for value in [0.25, 0.28, 0.32, 0.35]:
        variants.append({"variant": f"mom20_{value:.2f}", **BASE, "mom20_max": value})
    for value in [0.10, 0.12, 0.16, 0.18]:
        variants.append({"variant": f"mom5_{value:.2f}", **BASE, "mom5_max": value})
    for value in [1.60, 1.75, 2.05, 2.20]:
        variants.append({"variant": f"vol_{value:.2f}", **BASE, "vol_ratio_max": value})
    for value in [0.09, 0.10, 0.14, 0.16]:
        variants.append({"variant": f"rtmax_{value:.2f}", **BASE, "rt_return_max": value})
    for lo, hi in [(1.2, 7.0), (1.8, 7.0), (1.5, 6.0), (1.5, 8.0)]:
        variants.append({"variant": f"amount_{lo:.1f}_{hi:.1f}", **BASE, "amount_min": lo, "amount_max": hi})
    variants.extend(
        [
            {
                "variant": "tight_all",
                **BASE,
                "mom20_max": 0.28,
                "mom5_max": 0.12,
                "vol_ratio_max": 1.75,
                "rt_return_max": 0.10,
                "amount_min": 1.8,
                "amount_max": 6.0,
            },
            {
                "variant": "loose_all",
                **BASE,
                "mom20_max": 0.35,
                "mom5_max": 0.16,
                "vol_ratio_max": 2.05,
                "rt_return_max": 0.14,
                "amount_min": 1.2,
                "amount_max": 8.0,
            },
        ]
    )
    return variants


def _mask(d: pd.DataFrame, params: dict[str, Any]) -> pd.Series:
    return (
        pd.to_numeric(d["mom20"], errors="coerce").le(params["mom20_max"])
        & pd.to_numeric(d["mom5"], errors="coerce").le(params["mom5_max"])
        & pd.to_numeric(d["vol_ratio"], errors="coerce").le(params["vol_ratio_max"])
        & pd.to_numeric(d["rt_return_from_d1_close"], errors="coerce").between(params["rt_return_min"], params["rt_return_max"])
        & pd.to_numeric(d["rt_30m_amount_ratio"], errors="coerce").between(params["amount_min"], params["amount_max"])
    )


def _signal_summary(selected: pd.DataFrame, variant: dict[str, Any]) -> dict[str, Any]:
    return {
        **variant,
        "signals": int(selected["candidate_key"].nunique()),
        "days": int(selected["entry_date"].nunique()),
        "good_rate": float(selected["outcome_good"].mean()) if len(selected) else None,
        "bad_rate": float(selected["outcome_bad"].mean()) if len(selected) else None,
        "stop5_rate": float(selected["stop5_touch_30m"].mean()) if len(selected) else None,
        "avg_fwd5": float(pd.to_numeric(selected["outcome_fwd_ret_5d"], errors="coerce").mean()) if len(selected) else None,
        "avg_fwd10": float(pd.to_numeric(selected["outcome_fwd_ret_10d"], errors="coerce").mean()) if len(selected) else None,
        "avg_fwd20": float(pd.to_numeric(selected["outcome_fwd_ret_20d"], errors="coerce").mean()) if len(selected) else None,
        "target_precision": float(selected["is_same_day_target"].mean()) if len(selected) else None,
    }


def _write_report(output_dir: Path, signal_rows: list[dict[str, Any]], backtest_rows: list[dict[str, Any]]) -> None:
    lines = [
        "# G2 Risk Cool Parameter Sweep",
        "",
        "All variants use live-visible D-1 context plus intraday confirmation features. Outcome columns are used only for research labels.",
        "",
        "## Signal-Level Sweep",
        "",
        "| variant | signals | good | bad | stop5 | avg5 | avg10 | avg20 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in signal_rows:
        lines.append(
            f"| {row['variant']} | {row['signals']} | {_pct(row['good_rate'])} | {_pct(row['bad_rate'])} | "
            f"{_pct(row['stop5_rate'])} | {_pct(row['avg_fwd5'])} | {_pct(row['avg_fwd10'])} | {_pct(row['avg_fwd20'])} |"
        )
    if backtest_rows:
        lines.extend(
            [
                "",
                "## Portfolio Backtests",
                "",
                "| variant | signals | trades | total | excess | max_dd | win | avg_trade |",
                "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for row in backtest_rows:
            lines.append(
                f"| {row['variant']} | {row.get('signal_count', 0)} | {row.get('trade_count', 0)} | "
                f"{_pct(row.get('total_return'))} | {_pct(row.get('excess_return'))} | {_pct(row.get('max_drawdown'))} | "
                f"{_pct(row.get('win_rate'))} | {_pct(row.get('avg_trade_return'))} |"
            )
    (output_dir / "sweep_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    candidates = pd.read_parquet(args.candidates)
    variants = _variant_grid()
    signal_rows: list[dict[str, Any]] = []
    selected_by_variant: dict[str, pd.DataFrame] = {}
    for variant in variants:
        selected = candidates[_mask(candidates, variant).fillna(False)].copy()
        selected_by_variant[str(variant["variant"])] = selected
        signal_rows.append(_signal_summary(selected, variant))

    signal_df = pd.DataFrame(signal_rows).sort_values(["bad_rate", "avg_fwd10"], ascending=[True, False])
    signal_df.to_csv(output_dir / "signal_sweep.csv", index=False, encoding="utf-8-sig")

    backtest_rows: list[dict[str, Any]] = []
    if args.backtest:
        names = [x.strip() for x in args.backtest.split(",") if x.strip()]
        profile = FillProfile("gap_confirm_30m_close__intraday_30m_close", gap_open_mode="confirm_30m", intraday_mode="bar_close")
        for name in names:
            selected = selected_by_variant.get(name)
            if selected is None or selected.empty:
                continue
            source = output_dir / "sources" / f"{name}.parquet"
            source.parent.mkdir(parents=True, exist_ok=True)
            prepared = _prepare_source(selected)
            prepared.to_parquet(source, index=False)
            prepared.to_csv(source.with_suffix(".csv"), index=False, encoding="utf-8-sig")
            summary = _run_profile(
                signal_source=source,
                output_dir=output_dir / "backtests" / name,
                profile=profile,
                start_date=str(args.start_date),
                end_date=str(args.end_date),
                sort_mode="trigger_time",
            )
            summary["variant"] = name
            backtest_rows.append(summary)
        pd.DataFrame(backtest_rows).to_csv(output_dir / "backtest_sweep.csv", index=False, encoding="utf-8-sig")

    _write_report(output_dir, signal_df.to_dict("records"), backtest_rows)
    payload = {
        "schema_version": 1,
        "candidates": str(args.candidates),
        "signal_rows": signal_df.where(pd.notna(signal_df), None).to_dict("records"),
        "backtest_rows": pd.DataFrame(backtest_rows).where(pd.notna(pd.DataFrame(backtest_rows)), None).to_dict("records") if backtest_rows else [],
        "outputs": {
            "signal_sweep": "signal_sweep.csv",
            "backtest_sweep": "backtest_sweep.csv",
            "report": "sweep_report.md",
        },
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Parameter perturbation for the G2 risk_cool_v2 live-visible proxy rule.")
    parser.add_argument("--candidates", default=str(DEFAULT_CANDIDATES))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--start-date", default="2024-07-09")
    parser.add_argument("--end-date", default="2026-05-21")
    parser.add_argument("--backtest", default="")
    print(json.dumps(run(parser.parse_args()), ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()

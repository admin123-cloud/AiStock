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
DEFAULT_OUTPUT_DIR = ROOT / "reports" / "gen2_risk_cool_market_gate_sweep"


def _base_mask(d: pd.DataFrame, mom20_max: float = 0.30) -> pd.Series:
    return (
        pd.to_numeric(d["mom20"], errors="coerce").le(mom20_max)
        & pd.to_numeric(d["mom5"], errors="coerce").le(0.14)
        & pd.to_numeric(d["vol_ratio"], errors="coerce").le(1.90)
        & pd.to_numeric(d["rt_return_from_d1_close"], errors="coerce").between(0.00, 0.12)
        & pd.to_numeric(d["rt_30m_amount_ratio"], errors="coerce").between(1.5, 7.0)
    )


def _gate_masks(d: pd.DataFrame) -> dict[str, pd.Series]:
    close_ge_ma20 = d["index_close_ge_ma20"].fillna(False).astype(bool)
    mom20 = pd.to_numeric(d["index_mom20"], errors="coerce")
    breadth = pd.to_numeric(d["market_breadth"], errors="coerce")
    regime = d["legacy_regime"].astype(str)
    return {
        "base_no_gate": pd.Series(True, index=d.index),
        "idx_ge_ma20": close_ge_ma20,
        "idx_mom20_pos": mom20.gt(0),
        "idx_ge_ma20_and_mom20_pos": close_ge_ma20 & mom20.gt(0),
        "breadth_ge_55": breadth.ge(0.55),
        "breadth_ge_60": breadth.ge(0.60),
        "breadth_55_85": breadth.between(0.55, 0.85),
        "regime_trend_up": regime.eq("trend_up"),
        "regime_not_down": regime.ne("trend_down"),
        "trend_up_or_breadth_ge_60": regime.eq("trend_up") | breadth.ge(0.60),
        "defensive_combo": close_ge_ma20 & mom20.gt(0) & breadth.ge(0.55),
        "strong_combo": close_ge_ma20 & mom20.gt(0.02) & breadth.ge(0.60),
    }


def _segment_rows(equity_path: Path) -> list[dict[str, Any]]:
    e = pd.read_csv(equity_path)
    e["date"] = pd.to_datetime(e["date"], errors="coerce")
    e["strategy_equity"] = pd.to_numeric(e["strategy_equity"], errors="coerce")
    e["drawdown"] = pd.to_numeric(e["drawdown"], errors="coerce")
    windows = [
        ("2024H2-2025Q1", "2024-07-09", "2025-03-31"),
        ("2025Q2-Q4", "2025-04-01", "2025-12-31"),
        ("2026YTD", "2026-01-01", "2026-05-21"),
    ]
    rows = []
    for name, start, end in windows:
        g = e[(e["date"] >= pd.Timestamp(start)) & (e["date"] <= pd.Timestamp(end))].copy()
        if g.empty:
            continue
        first = float(g.iloc[0]["strategy_equity"])
        last = float(g.iloc[-1]["strategy_equity"])
        rows.append(
            {
                "segment": name,
                "return": last / first - 1.0 if first else None,
                "max_drawdown": float(g["drawdown"].min()),
            }
        )
    return rows


def _signal_summary(selected: pd.DataFrame, gate: str, variant: str) -> dict[str, Any]:
    return {
        "variant": variant,
        "gate": gate,
        "signals": int(selected["candidate_key"].nunique()),
        "days": int(selected["entry_date"].nunique()),
        "good_rate": float(selected["outcome_good"].mean()) if len(selected) else None,
        "bad_rate": float(selected["outcome_bad"].mean()) if len(selected) else None,
        "stop5_rate": float(selected["stop5_touch_30m"].mean()) if len(selected) else None,
        "avg_fwd5": float(pd.to_numeric(selected["outcome_fwd_ret_5d"], errors="coerce").mean()) if len(selected) else None,
        "avg_fwd10": float(pd.to_numeric(selected["outcome_fwd_ret_10d"], errors="coerce").mean()) if len(selected) else None,
        "avg_fwd20": float(pd.to_numeric(selected["outcome_fwd_ret_20d"], errors="coerce").mean()) if len(selected) else None,
    }


def _write_report(output_dir: Path, rows: list[dict[str, Any]]) -> None:
    lines = [
        "# G2 Risk Cool Market Gate Sweep",
        "",
        "Candidate rule is fixed to risk_cool base unless variant says otherwise. Market fields come from candidate context and are treated as D-1 visible context.",
        "",
        "| variant | gate | signals | trades | total | excess | max_dd | win | avg_trade |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            f"| {row['variant']} | {row['gate']} | {row.get('signal_count', 0)} | {row.get('trade_count', 0)} | "
            f"{_pct(row.get('total_return'))} | {_pct(row.get('excess_return'))} | {_pct(row.get('max_drawdown'))} | "
            f"{_pct(row.get('win_rate'))} | {_pct(row.get('avg_trade_return'))} |"
        )
    (output_dir / "market_gate_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    candidates = pd.read_parquet(args.candidates)
    candidates["entry_date"] = pd.to_datetime(candidates["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    variants = {"base": _base_mask(candidates, 0.30), "mom20_0.35": _base_mask(candidates, 0.35)}
    gates = _gate_masks(candidates)
    wanted_gates = [x.strip() for x in str(args.gates).split(",") if x.strip()] if args.gates else list(gates)
    wanted_variants = [x.strip() for x in str(args.variants).split(",") if x.strip()] if args.variants else list(variants)
    profile = FillProfile("gap_confirm_30m_close__intraday_30m_close", gap_open_mode="confirm_30m", intraday_mode="bar_close")
    signal_rows: list[dict[str, Any]] = []
    backtest_rows: list[dict[str, Any]] = []
    segment_rows: list[dict[str, Any]] = []

    for variant in wanted_variants:
        if variant not in variants:
            raise ValueError(f"Unknown variant: {variant}")
        for gate in wanted_gates:
            if gate not in gates:
                raise ValueError(f"Unknown gate: {gate}")
            selected = candidates[(variants[variant] & gates[gate]).fillna(False)].copy()
            signal_rows.append(_signal_summary(selected, gate, variant))
            if selected.empty or len(selected) < int(args.min_signals):
                continue
            key = f"{variant}__{gate}"
            source = output_dir / "sources" / f"{key}.parquet"
            source.parent.mkdir(parents=True, exist_ok=True)
            prepared = _prepare_source(selected)
            prepared.to_parquet(source, index=False)
            prepared.to_csv(source.with_suffix(".csv"), index=False, encoding="utf-8-sig")
            run_dir = output_dir / "backtests" / key
            summary = _run_profile(
                signal_source=source,
                output_dir=run_dir,
                profile=profile,
                start_date=str(args.start_date),
                end_date=str(args.end_date),
                sort_mode="trigger_time",
            )
            summary["variant"] = variant
            summary["gate"] = gate
            backtest_rows.append(summary)
            for seg in _segment_rows(run_dir / "equity_curve.csv"):
                segment_rows.append({"variant": variant, "gate": gate, **seg})

    signal_df = pd.DataFrame(signal_rows)
    backtest_df = pd.DataFrame(backtest_rows)
    if not backtest_df.empty:
        backtest_df = backtest_df.sort_values(["excess_return", "max_drawdown"], ascending=[False, False])
    segment_df = pd.DataFrame(segment_rows)
    signal_df.to_csv(output_dir / "signal_gate_summary.csv", index=False, encoding="utf-8-sig")
    backtest_df.to_csv(output_dir / "backtest_gate_summary.csv", index=False, encoding="utf-8-sig")
    segment_df.to_csv(output_dir / "segment_gate_summary.csv", index=False, encoding="utf-8-sig")
    _write_report(output_dir, backtest_df.to_dict("records") if not backtest_df.empty else [])
    payload = {
        "schema_version": 1,
        "rows": backtest_df.where(pd.notna(backtest_df), None).to_dict("records") if not backtest_df.empty else [],
        "outputs": {
            "signal_summary": "signal_gate_summary.csv",
            "backtest_summary": "backtest_gate_summary.csv",
            "segment_summary": "segment_gate_summary.csv",
            "report": "market_gate_report.md",
        },
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Market gate sweep for risk_cool live-visible G2 candidate rule.")
    parser.add_argument("--candidates", default=str(DEFAULT_CANDIDATES))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--start-date", default="2024-07-09")
    parser.add_argument("--end-date", default="2026-05-21")
    parser.add_argument("--variants", default="base")
    parser.add_argument("--gates", default="")
    parser.add_argument("--min-signals", default=150, type=int)
    print(json.dumps(run(parser.parse_args()), ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()

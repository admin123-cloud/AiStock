from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen2_backtest_open_v1_portfolio import _json_default  # noqa: E402
from scripts.gen2_backtest_risk_cool_dynamic_circuit import _run_dynamic  # noqa: E402


WF_WINDOWS = [
    {
        "fold": "validation_2025q2_q4",
        "train_start": "2024-07-09",
        "train_end": "2025-03-31",
        "test_start": "2025-04-01",
        "test_end": "2025-12-31",
    },
    {
        "fold": "blind_2026ytd",
        "train_start": "2024-07-09",
        "train_end": "2025-12-31",
        "test_start": "2026-01-01",
        "test_end": "2026-05-21",
    },
]


def _sharpe(curve: pd.DataFrame) -> float | None:
    equity = pd.to_numeric(curve["strategy_equity"], errors="coerce")
    rets = equity.pct_change().dropna()
    if len(rets) < 2:
        return None
    std = float(rets.std(ddof=1))
    if std <= 0 or not math.isfinite(std):
        return None
    return float(rets.mean() / std * math.sqrt(252))


def _candidate_directions(d: pd.DataFrame, factors: list[str], target: str, train_start: str, train_end: str) -> dict[str, str]:
    train = d[(d["date"] >= pd.Timestamp(train_start)) & (d["date"] <= pd.Timestamp(train_end))].copy()
    train[target] = pd.to_numeric(train[target], errors="coerce")
    directions: dict[str, str] = {}
    for fid in factors:
        ics: list[float] = []
        for _, day in train.dropna(subset=[fid, target]).groupby("entry_date"):
            if len(day) < 2 or day[fid].nunique() < 2:
                continue
            ic = day[fid].rank().corr(day[target].rank())
            if pd.notna(ic) and math.isfinite(float(ic)):
                ics.append(float(ic))
        directions[fid] = "high" if (float(np.mean(ics)) if ics else 0.0) >= 0 else "low"
    return directions


def _score_with_train_threshold(
    d: pd.DataFrame,
    factors: list[str],
    directions: dict[str, str],
    threshold_q: float,
    train_start: str,
    train_end: str,
    test_start: str,
    test_end: str,
) -> tuple[pd.DataFrame, float]:
    scored = d.copy()
    cols: list[str] = []
    train_mask = (scored["date"] >= pd.Timestamp(train_start)) & (scored["date"] <= pd.Timestamp(train_end))
    for fid in factors:
        raw = pd.to_numeric(scored[fid], errors="coerce")
        oriented = raw if directions.get(fid, "high") == "high" else -raw
        train_values = oriented[train_mask].dropna().sort_values().to_numpy(dtype=float)
        if len(train_values) == 0:
            continue
        col = f"{fid}_train_pct"
        values = oriented.to_numpy(dtype=float)
        scored[col] = np.searchsorted(train_values, values, side="right") / float(len(train_values))
        scored.loc[oriented.isna(), col] = np.nan
        cols.append(col)
    if not cols:
        raise RuntimeError("No Alpha191 train percentile columns available.")
    scored["alpha191_gate_score"] = scored[cols].mean(axis=1)
    train_scores = scored.loc[train_mask, "alpha191_gate_score"].dropna()
    if train_scores.empty:
        raise RuntimeError(f"No train scores for {train_start}..{train_end}")
    threshold = float(train_scores.quantile(float(threshold_q)))
    test_mask = (scored["date"] >= pd.Timestamp(test_start)) & (scored["date"] <= pd.Timestamp(test_end))
    kept = scored[test_mask & (scored["alpha191_gate_score"] >= threshold)].copy()
    kept["alpha191_gate_threshold"] = threshold
    kept["alpha191_gate_factors"] = ",".join(factors)
    return kept, threshold


def _run_one(source_path: Path, output_dir: Path, policy: str, start_date: str, end_date: str) -> dict[str, Any]:
    summary = _run_dynamic(source_path, output_dir, policy, start_date, end_date, sort_mode="trigger_time")
    curve = pd.read_csv(output_dir / "equity_curve.csv")
    return {
        "signals": summary.get("signal_count"),
        "trades": summary.get("trade_count"),
        "total_return": summary.get("total_return"),
        "max_drawdown": summary.get("max_drawdown"),
        "win_rate": summary.get("win_rate"),
        "avg_trade_return": summary.get("avg_trade_return"),
        "daily_sharpe": _sharpe(curve),
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    source = pd.read_parquet(args.source)
    values = pd.read_parquet(args.values)
    source["date"] = pd.to_datetime(source["entry_date"], errors="coerce")
    source["code6"] = source["code"].astype(str).str[:6]
    merged = source.merge(values, on=["date", "code6"], how="left")
    factors = [item.strip() for item in str(args.factors).split(",") if item.strip()]
    policies = [item.strip() for item in str(args.policies).split(",") if item.strip()]
    qs = [float(item) for item in str(args.threshold_qs).split(",") if item.strip()]

    rows: list[dict[str, Any]] = []
    for fold in WF_WINDOWS:
        fold_name = fold["fold"]
        baseline = merged[
            (merged["date"] >= pd.Timestamp(fold["test_start"]))
            & (merged["date"] <= pd.Timestamp(fold["test_end"]))
        ].copy()
        baseline_path = output_dir / "sources" / f"{fold_name}_baseline.parquet"
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline.drop(columns=["date", "code6"]).to_parquet(baseline_path, index=False)
        for policy in policies:
            baseline_metrics = _run_one(
                baseline_path,
                output_dir / "backtests" / fold_name / "baseline" / policy,
                policy,
                fold["test_start"],
                fold["test_end"],
            )
            rows.append({"fold": fold_name, "variant": "baseline", "policy": policy, **baseline_metrics})

        directions = _candidate_directions(
            merged,
            factors,
            str(args.target),
            fold["train_start"],
            fold["train_end"],
        )
        for q in qs:
            kept, threshold = _score_with_train_threshold(
                merged,
                factors,
                directions,
                q,
                fold["train_start"],
                fold["train_end"],
                fold["test_start"],
                fold["test_end"],
            )
            variant = f"alpha191_gate_keep{int(round((1.0 - q) * 100))}"
            source_path = output_dir / "sources" / f"{fold_name}_{variant}.parquet"
            kept.drop(columns=["date", "code6"]).to_parquet(source_path, index=False)
            for policy in policies:
                metrics = _run_one(
                    source_path,
                    output_dir / "backtests" / fold_name / variant / policy,
                    policy,
                    fold["test_start"],
                    fold["test_end"],
                )
                rows.append(
                    {
                        "fold": fold_name,
                        "variant": variant,
                        "policy": policy,
                        "factors": ",".join(factors),
                        "directions": json.dumps(directions, ensure_ascii=False, sort_keys=True),
                        "threshold_q": q,
                        "threshold": threshold,
                        "train_start": fold["train_start"],
                        "train_end": fold["train_end"],
                        "test_start": fold["test_start"],
                        "test_end": fold["test_end"],
                        **metrics,
                    }
                )

    result = pd.DataFrame(rows)
    result.to_csv(output_dir / "walk_forward_summary.csv", index=False, encoding="utf-8-sig")
    payload = {"schema_version": 1, "rows": result.where(pd.notna(result), None).to_dict("records")}
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Walk-forward test for G2 Alpha191 candidate-pool gates.")
    parser.add_argument("--source", default=str(ROOT / "reports" / "gen2_alpha191_overlay_candidate_train_dirs" / "sources" / "risk_cool_base.parquet"))
    parser.add_argument("--values", default=str(ROOT / "reports" / "gen2_alpha191_overlay_candidate_train_dirs" / "alpha191_signal_values.parquet"))
    parser.add_argument("--output-dir", default=str(ROOT / "reports" / "gen2_alpha191_gate_walk_forward"))
    parser.add_argument("--factors", default="Alpha150,Alpha095,Alpha144")
    parser.add_argument("--target", default="outcome_fwd_ret_10d")
    parser.add_argument("--threshold-qs", default="0.2")
    parser.add_argument("--policies", default="stop_cd3_skip,two_stop_cd3_skip")
    print(json.dumps(run(parser.parse_args()), ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()

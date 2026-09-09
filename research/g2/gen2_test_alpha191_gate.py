from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen2_backtest_open_v1_portfolio import _json_default  # noqa: E402
from scripts.gen2_backtest_risk_cool_dynamic_circuit import _run_dynamic  # noqa: E402


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


def _build_gate_source(
    merged: pd.DataFrame,
    factors: list[str],
    directions: dict[str, str],
    threshold_q: float,
    train_start: str,
    train_end: str,
) -> tuple[pd.DataFrame, float]:
    d = merged.copy()
    cols: list[str] = []
    train_mask = (d["date"] >= pd.Timestamp(train_start)) & (d["date"] <= pd.Timestamp(train_end))
    for fid in factors:
        raw = pd.to_numeric(d[fid], errors="coerce")
        oriented = raw if directions.get(fid, "high") == "high" else -raw
        train_values = oriented[train_mask].dropna().sort_values().to_numpy(dtype=float)
        if len(train_values) == 0:
            continue
        col = f"{fid}_train_pct"
        values = oriented.to_numpy(dtype=float)
        d[col] = np.searchsorted(train_values, values, side="right") / float(len(train_values))
        d.loc[oriented.isna(), col] = np.nan
        cols.append(col)
    if not cols:
        raise RuntimeError("No Alpha191 train percentile columns available.")
    d["alpha191_gate_score"] = d[cols].mean(axis=1)
    train_scores = d.loc[train_mask, "alpha191_gate_score"].dropna()
    if train_scores.empty:
        raise RuntimeError("No train scores available for Alpha191 gate.")
    threshold = float(train_scores.quantile(float(threshold_q)))
    kept = d[d["alpha191_gate_score"] >= threshold].copy()
    kept["alpha191_gate_threshold"] = threshold
    kept["alpha191_gate_factors"] = ",".join(factors)
    return kept, threshold


def run(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    source = pd.read_parquet(args.source)
    values = pd.read_parquet(args.values)
    source["date"] = pd.to_datetime(source["entry_date"], errors="coerce")
    source["code6"] = source["code"].astype(str).str[:6]
    merged = source.merge(values, on=["date", "code6"], how="left")
    factors = [x.strip() for x in str(args.factors).split(",") if x.strip()]
    if str(getattr(args, "directions", "") or "").strip():
        directions = {}
        for item in str(args.directions).split(","):
            if ":" not in item:
                continue
            key, value = item.split(":", 1)
            directions[key.strip()] = value.strip()
    elif str(getattr(args, "directions_json", "") or "").strip():
        directions = json.loads(str(args.directions_json))
    else:
        directions = _candidate_directions(merged, factors, str(args.target), str(args.train_start), str(args.train_end))

    rows: list[dict[str, Any]] = []
    for q in [float(x) for x in str(args.threshold_qs).split(",") if x.strip()]:
        kept, threshold = _build_gate_source(merged, factors, directions, q, str(args.train_start), str(args.train_end))
        variant_name = f"alpha191_gate_keep{int(round((1.0 - q) * 100))}"
        variant_dir = output_dir / "variant_sources"
        variant_dir.mkdir(parents=True, exist_ok=True)
        variant_path = variant_dir / f"{variant_name}.parquet"
        kept.drop(columns=["date", "code6"]).to_parquet(variant_path, index=False)
        for policy in [x.strip() for x in str(args.policies).split(",") if x.strip()]:
            run_dir = output_dir / "backtests" / f"{variant_name}_{policy}" / policy
            summary = _run_dynamic(
                variant_path,
                run_dir,
                policy,
                str(args.start_date),
                str(args.end_date),
                sort_mode="trigger_time",
            )
            curve = pd.read_csv(run_dir / "equity_curve.csv")
            rows.append(
                {
                    "variant": variant_name,
                    "policy": policy,
                    "factors": ",".join(factors),
                    "directions": json.dumps(directions, ensure_ascii=False, sort_keys=True),
                    "threshold_q": q,
                    "threshold": threshold,
                    "signals": int(len(kept)),
                    "trades": summary.get("trade_count"),
                    "total_return": summary.get("total_return"),
                    "max_drawdown": summary.get("max_drawdown"),
                    "win_rate": summary.get("win_rate"),
                    "avg_trade_return": summary.get("avg_trade_return"),
                    "daily_sharpe": _sharpe(curve),
                }
            )
    result = pd.DataFrame(rows).sort_values(["daily_sharpe", "total_return"], ascending=[False, False])
    result.to_csv(output_dir / "alpha191_gate_summary.csv", index=False, encoding="utf-8-sig")
    payload = {
        "schema_version": 1,
        "directions": directions,
        "rows": result.where(pd.notna(result), None).to_dict("records"),
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Test Alpha191 candidate-pool gate on G2 risk_cool signals.")
    parser.add_argument("--source", default=str(_report_path() / "gen2_alpha191_overlay_candidate_train_dirs" / "sources" / "risk_cool_base.parquet"))
    parser.add_argument("--values", default=str(_report_path() / "gen2_alpha191_overlay_candidate_train_dirs" / "alpha191_signal_values.parquet"))
    parser.add_argument("--output-dir", default=str(_report_path() / "gen2_alpha191_gate"))
    parser.add_argument("--factors", default="Alpha150,Alpha095,Alpha144")
    parser.add_argument("--directions", default="")
    parser.add_argument("--directions-json", default="")
    parser.add_argument("--target", default="outcome_fwd_ret_10d")
    parser.add_argument("--train-start", default="2024-07-09")
    parser.add_argument("--train-end", default="2025-03-31")
    parser.add_argument("--start-date", default="2024-07-09")
    parser.add_argument("--end-date", default="2026-05-21")
    parser.add_argument("--threshold-qs", default="0.2")
    parser.add_argument("--policies", default="stop_cd3_skip")
    print(json.dumps(run(parser.parse_args()), ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()

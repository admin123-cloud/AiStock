from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

DEFAULT_EVENT_DATASET = REPO_ROOT / "reports" / "gen2_event_study_full" / "v4_event_dataset.parquet"
DEFAULT_STATE_DAILY = REPO_ROOT / "reports" / "gen2_open_state_research_full" / "g2_open_state_daily.csv"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "reports" / "gen2_pattern_state_matrix"
HORIZONS = (1, 2, 3, 5, 10, 20)


def _json_default(value: Any) -> Any:
    if isinstance(value, (pd.Timestamp,)):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if pd.isna(value):
        return None
    return str(value)


def _num(df: pd.DataFrame, col: str, default: float = np.nan) -> pd.Series:
    if col not in df.columns:
        return pd.Series(default, index=df.index, dtype="float64")
    return pd.to_numeric(df[col], errors="coerce")


def _load_inputs(event_dataset: Path, state_daily: Path) -> pd.DataFrame:
    if not event_dataset.exists():
        raise FileNotFoundError(f"Missing event dataset: {event_dataset}")
    if not state_daily.exists():
        raise FileNotFoundError(f"Missing state daily: {state_daily}")
    events = pd.read_parquet(event_dataset)
    states = pd.read_csv(state_daily)
    events["trade_date"] = pd.to_datetime(events["trade_date"]).dt.strftime("%Y-%m-%d")
    states["trade_date"] = pd.to_datetime(states["trade_date"]).dt.strftime("%Y-%m-%d")
    merge_cols = ["trade_date", "g2_open_state"]
    for horizon in HORIZONS:
        col = f"csi1000_fwd_ret_{horizon}d"
        if col in states.columns:
            merge_cols.append(col)
    out = events.merge(states[merge_cols], on="trade_date", how="left")
    out = _add_trade_gap(out, states["trade_date"].tolist())
    return out


def _add_trade_gap(df: pd.DataFrame, trade_dates: List[str]) -> pd.DataFrame:
    date_index = {date: idx for idx, date in enumerate(trade_dates)}
    out = df.copy()
    out["previous_signal_date"] = pd.to_datetime(out["previous_signal_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    out["trade_idx"] = out["trade_date"].map(date_index)
    out["previous_trade_idx"] = out["previous_signal_date"].map(date_index)
    out["trade_gap"] = out["trade_idx"] - out["previous_trade_idx"]
    return out


def _pattern_masks(df: pd.DataFrame) -> Dict[str, pd.Series]:
    entry = df["entry_pass"].fillna(False).astype(bool)
    rank = _num(df, "v4_rank")
    previous_rank = _num(df, "previous_rank")
    rank_change = _num(df, "rank_change")
    score = _num(df, "v4_score")
    score_change = _num(df, "score_change")
    pool_streak = _num(df, "pool_streak").fillna(0)
    mom5 = _num(df, "mom5")
    mom10 = _num(df, "mom10")
    mom20 = _num(df, "mom20")
    vol_ratio = _num(df, "vol_ratio")
    vol10 = _num(df, "vol10")
    trade_gap = _num(df, "trade_gap")
    rank_status = df.get("rank_change_status", pd.Series("", index=df.index)).fillna("")

    top10 = entry & rank.le(10)
    top30 = entry & rank.le(30)
    top50 = entry & rank.le(50)
    high_score_core = entry & score.ge(0.82) & rank.le(30) & pool_streak.ge(2)
    high_score_core_cool = (
        high_score_core
        & score.le(0.93)
        & pool_streak.le(15)
        & mom5.ge(0)
        & mom5.le(0.18)
        & mom10.le(0.35)
        & vol_ratio.le(2.20)
        & vol10.le(0.060)
    )
    high_score_core_steady = (
        entry
        & score.ge(0.82)
        & score.le(0.92)
        & rank.le(20)
        & pool_streak.ge(3)
        & pool_streak.le(12)
        & score_change.ge(-0.03)
        & mom5.ge(0)
        & mom5.le(0.12)
        & vol10.le(0.055)
    )
    rank_acceleration = entry & previous_rank.notna() & (
        ((rank_change.ge(20)) & score_change.gt(0))
        | (previous_rank.gt(30) & rank.le(20))
        | (previous_rank.gt(50) & rank.le(30))
    )
    rank_acceleration_strict = entry & previous_rank.notna() & rank_change.ge(50) & score_change.ge(0.03) & rank.le(30)
    new_top30 = entry & rank_status.eq("new") & rank.le(30)
    new_top50 = entry & rank_status.eq("new") & rank.le(50)
    removed_return = entry & previous_rank.notna() & trade_gap.ge(2) & rank.le(50) & score_change.gt(0)
    removed_return_quality = (
        removed_return
        & trade_gap.le(10)
        & score.ge(0.80)
        & score.le(0.92)
        & score_change.ge(0.03)
        & rank_change.ge(20)
        & mom5.ge(0)
        & mom10.ge(0)
        & vol10.le(0.060)
    )
    removed_return_strict = (
        removed_return_quality
        & trade_gap.le(8)
        & rank.le(30)
        & score.ge(0.82)
        & score_change.ge(0.05)
        & rank_change.ge(50)
        & mom5.le(0.12)
        & vol_ratio.ge(0.80)
        & vol_ratio.le(2.20)
    )
    momentum_pullback_restart = (
        entry
        & mom10.ge(0.08)
        & mom20.ge(0)
        & mom5.ge(0)
        & mom5.le(0.08)
        & mom5.le(mom10 * 0.60)
        & vol_ratio.ge(0.80)
        & vol_ratio.le(2.50)
        & vol10.le(0.065)
    )
    momentum_cool_top30 = (
        top30
        & mom10.ge(0.05)
        & mom20.ge(0)
        & mom5.ge(-0.03)
        & mom5.le(0.08)
        & vol10.le(0.070)
    )
    low_vol_top30 = top30 & vol10.le(0.045) & mom20.ge(0)
    score_mid_entry = entry & score.ge(0.75) & score.lt(0.85)
    score_high_entry = entry & score.ge(0.85)

    return {
        "score_pool": pd.Series(True, index=df.index),
        "entry_all": entry,
        "entry_top10": top10,
        "entry_top30": top30,
        "entry_top50": top50,
        "score_mid_entry": score_mid_entry,
        "score_high_entry": score_high_entry,
        "high_score_core": high_score_core,
        "high_score_core_cool": high_score_core_cool,
        "high_score_core_steady": high_score_core_steady,
        "rank_acceleration": rank_acceleration,
        "rank_acceleration_strict": rank_acceleration_strict,
        "new_top30": new_top30,
        "new_top50": new_top50,
        "removed_return": removed_return,
        "removed_return_quality": removed_return_quality,
        "removed_return_strict": removed_return_strict,
        "momentum_pullback_restart": momentum_pullback_restart,
        "momentum_cool_top30": momentum_cool_top30,
        "low_vol_top30": low_vol_top30,
    }


def _stats(values: pd.Series) -> Dict[str, Any]:
    x = pd.to_numeric(values, errors="coerce").dropna()
    if x.empty:
        return {
            "count": 0,
            "mean": None,
            "median": None,
            "win_rate": None,
            "p25": None,
            "p75": None,
        }
    return {
        "count": int(len(x)),
        "mean": float(x.mean()),
        "median": float(x.median()),
        "win_rate": float((x > 0).mean()),
        "p25": float(x.quantile(0.25)),
        "p75": float(x.quantile(0.75)),
    }


def _summarize_matrix(events: pd.DataFrame, min_count: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows: List[Dict[str, Any]] = []
    masks = _pattern_masks(events)
    for pattern, mask in masks.items():
        part = events[mask.fillna(False)].copy()
        if part.empty:
            continue
        for state, group in part.groupby("g2_open_state", dropna=False):
            state_name = state if pd.notna(state) else "UNKNOWN"
            for horizon in HORIZONS:
                ret_col = f"fwd_ret_{horizon}d"
                idx_col = f"csi1000_fwd_ret_{horizon}d"
                if ret_col not in group.columns:
                    continue
                raw = _stats(group[ret_col])
                excess = (
                    _stats(pd.to_numeric(group[ret_col], errors="coerce") - pd.to_numeric(group[idx_col], errors="coerce"))
                    if idx_col in group.columns
                    else {"mean": None, "median": None, "win_rate": None}
                )
                rows.append(
                    {
                        "pattern": pattern,
                        "state": state_name,
                        "horizon": horizon,
                        "count": raw["count"],
                        "mean": raw["mean"],
                        "median": raw["median"],
                        "win_rate": raw["win_rate"],
                        "p25": raw["p25"],
                        "p75": raw["p75"],
                        "excess_mean_vs_csi1000": excess["mean"],
                        "excess_median_vs_csi1000": excess["median"],
                        "excess_win_rate_vs_csi1000": excess["win_rate"],
                    }
                )
    matrix = pd.DataFrame(rows)
    if matrix.empty:
        return matrix, matrix
    candidates = matrix[
        (matrix["count"] >= int(min_count))
        & (matrix["mean"] > 0)
        & (matrix["median"] > 0)
        & (matrix["win_rate"] >= 0.50)
    ].copy()
    if not candidates.empty:
        candidates["score"] = (
            candidates["mean"].fillna(0) * 100.0
            + candidates["median"].fillna(0) * 60.0
            + (candidates["win_rate"].fillna(0) - 0.50) * 10.0
            + candidates["excess_mean_vs_csi1000"].fillna(0) * 30.0
        )
        candidates = candidates.sort_values(["score", "count"], ascending=[False, False])
    return matrix.sort_values(["pattern", "state", "horizon"]), candidates


def _write_findings(output_dir: Path, candidates: pd.DataFrame, matrix: pd.DataFrame) -> None:
    lines = ["# G2 Pattern x State Matrix Findings", ""]
    if candidates.empty:
        lines.append("No candidate cells passed the default filters.")
    else:
        lines.extend(
            [
                "## Candidate Cells",
                "",
                "| pattern | state | horizon | count | mean | median | win_rate | excess_mean |",
                "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for _, row in candidates.head(30).iterrows():
            lines.append(
                "| {pattern} | {state} | {horizon} | {count} | {mean:.2%} | {median:.2%} | {win_rate:.2%} | {excess:.2%} |".format(
                    pattern=row["pattern"],
                    state=row["state"],
                    horizon=int(row["horizon"]),
                    count=int(row["count"]),
                    mean=float(row["mean"]),
                    median=float(row["median"]),
                    win_rate=float(row["win_rate"]),
                    excess=float(row["excess_mean_vs_csi1000"]) if pd.notna(row["excess_mean_vs_csi1000"]) else 0.0,
                )
            )
    lines.extend(["", "## Notes", "", "- This is an event-level matrix, not a portfolio backtest.", "- Candidate cells still need month/year stability and trade-concentration checks."])
    (output_dir / "findings.md").write_text("\n".join(lines), encoding="utf-8")


def run(event_dataset: Path, state_daily: Path, output_dir: Path, min_count: int) -> Dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    events = _load_inputs(event_dataset, state_daily)
    matrix, candidates = _summarize_matrix(events, min_count=min_count)
    matrix.to_csv(output_dir / "pattern_state_matrix.csv", index=False, encoding="utf-8-sig")
    candidates.to_csv(output_dir / "candidate_cells.csv", index=False, encoding="utf-8-sig")
    _write_findings(output_dir, candidates, matrix)
    payload = {
        "schema_version": 1,
        "event_dataset": str(event_dataset),
        "state_daily": str(state_daily),
        "min_count": int(min_count),
        "rows": int(len(events)),
        "date_min": str(events["trade_date"].min()),
        "date_max": str(events["trade_date"].max()),
        "matrix_rows": int(len(matrix)),
        "candidate_rows": int(len(candidates)),
        "outputs": {
            "matrix": "pattern_state_matrix.csv",
            "candidates": "candidate_cells.csv",
            "findings": "findings.md",
        },
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Find profitable G2 pattern x open-state matrix cells.")
    parser.add_argument("--event-dataset", default=str(DEFAULT_EVENT_DATASET))
    parser.add_argument("--state-daily", default=str(DEFAULT_STATE_DAILY))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--min-count", type=int, default=200)
    args = parser.parse_args()
    payload = run(
        event_dataset=Path(args.event_dataset),
        state_daily=Path(args.state_daily),
        output_dir=Path(args.output_dir),
        min_count=int(args.min_count),
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()

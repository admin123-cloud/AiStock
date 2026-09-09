from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import argparse
import json
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pandas as pd

REPO_ROOT = _PROJECT_ROOT
DEFAULT_INPUT = _report_path() / "gen2_30m_fractal_restart_expanded_w2" / "fractal_triggers.parquet"
DEFAULT_OUTPUT_DIR = _report_path() / "gen2_30m_candidate_stability"


DEFAULT_CANDIDATES = [
    ("normal_rank100_break_high_ma5_vol_h3", "pullback_restart_rank100", "NORMAL", "bottom_fractal_break_high_ma5_vol", 3),
    ("normal_rank100_break_high_vol_h3", "pullback_restart_rank100", "NORMAL", "bottom_fractal_break_high_vol", 3),
    ("normal_rank100_break_high_ma5_vol_h5", "pullback_restart_rank100", "NORMAL", "bottom_fractal_break_high_ma5_vol", 5),
    ("normal_rank100_break_high_vol_h5", "pullback_restart_rank100", "NORMAL", "bottom_fractal_break_high_vol", 5),
    ("aggressive_rank100_reclaim_ma5_h20", "pullback_restart_rank100", "AGGRESSIVE", "bottom_fractal_reclaim_ma5", 20),
    ("aggressive_rank100_break_high_ma10_h10", "pullback_restart_rank100", "AGGRESSIVE", "bottom_fractal_break_high_ma10", 10),
    ("probe_rank100_break_high_vol_h20", "pullback_restart_rank100", "PROBE", "bottom_fractal_break_high_vol", 20),
]


from research.common.reporting import numpy_json_default as _json_default


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
            "sum": None,
        }
    return {
        "count": int(len(x)),
        "mean": float(x.mean()),
        "median": float(x.median()),
        "win_rate": float((x > 0).mean()),
        "p25": float(x.quantile(0.25)),
        "p75": float(x.quantile(0.75)),
        "sum": float(x.sum()),
    }


def _filter_candidate(df: pd.DataFrame, pattern: str, state: str, trigger_type: str) -> pd.DataFrame:
    return df[
        (df["pattern"] == pattern)
        & (df["g2_open_state"] == state)
        & (df["trigger_type"] == trigger_type)
    ].copy()


def _overall_row(name: str, d: pd.DataFrame, horizon: int) -> Dict[str, Any]:
    ret_col = f"entry_fwd_ret_{horizon}d"
    excess_col = f"entry_excess_vs_csi1000_{horizon}d"
    raw = _stats(d[ret_col])
    excess = _stats(d[excess_col]) if excess_col in d.columns else {}
    row = {
        "candidate": name,
        "horizon": horizon,
        "unique_codes": int(d["code"].nunique()) if not d.empty else 0,
        "start_entry_date": str(d["entry_date"].min()) if not d.empty else None,
        "end_entry_date": str(d["entry_date"].max()) if not d.empty else None,
    }
    row.update({f"raw_{k}": v for k, v in raw.items()})
    row.update({f"excess_{k}": v for k, v in excess.items()})
    return row


def _monthly_rows(name: str, d: pd.DataFrame, horizon: int) -> List[Dict[str, Any]]:
    ret_col = f"entry_fwd_ret_{horizon}d"
    excess_col = f"entry_excess_vs_csi1000_{horizon}d"
    out = d.copy()
    out["entry_month"] = pd.to_datetime(out["entry_date"]).dt.strftime("%Y-%m")
    rows: List[Dict[str, Any]] = []
    for month, g in out.groupby("entry_month", sort=True, observed=True):
        raw = _stats(g[ret_col])
        excess = _stats(g[excess_col]) if excess_col in g.columns else {}
        row = {"candidate": name, "entry_month": month, "horizon": horizon, "unique_codes": int(g["code"].nunique())}
        row.update({f"raw_{k}": v for k, v in raw.items()})
        row.update({f"excess_{k}": v for k, v in excess.items()})
        rows.append(row)
    return rows


def _code_rows(name: str, d: pd.DataFrame, horizon: int) -> pd.DataFrame:
    ret_col = f"entry_fwd_ret_{horizon}d"
    excess_col = f"entry_excess_vs_csi1000_{horizon}d"
    rows: List[Dict[str, Any]] = []
    total_abs = float(pd.to_numeric(d[ret_col], errors="coerce").abs().sum())
    for (code, stock_name), g in d.groupby(["code", "name"], dropna=False):
        raw = _stats(g[ret_col])
        excess = _stats(g[excess_col]) if excess_col in g.columns else {}
        rows.append(
            {
                "candidate": name,
                "code": code,
                "name": stock_name,
                "horizon": horizon,
                "trade_count": int(len(g)),
                "raw_sum": raw["sum"],
                "raw_mean": raw["mean"],
                "raw_median": raw["median"],
                "raw_win_rate": raw["win_rate"],
                "excess_sum": excess.get("sum"),
                "excess_mean": excess.get("mean"),
                "abs_raw_share": abs(float(raw["sum"] or 0.0)) / total_abs if total_abs > 0 else 0.0,
            }
        )
    return pd.DataFrame(rows).sort_values(["candidate", "raw_sum"], ascending=[True, False])


def _leave_top_rows(name: str, d: pd.DataFrame, horizon: int) -> List[Dict[str, Any]]:
    ret_col = f"entry_fwd_ret_{horizon}d"
    excess_col = f"entry_excess_vs_csi1000_{horizon}d"
    valid = d.dropna(subset=[ret_col]).copy()
    rows: List[Dict[str, Any]] = []
    for n in [1, 3, 5, 10, 20]:
        winners = valid.sort_values(ret_col, ascending=False).head(n)
        remain = valid.drop(index=winners.index)
        raw = _stats(remain[ret_col])
        excess = _stats(remain[excess_col]) if excess_col in remain.columns else {}
        rows.append(
            {
                "candidate": name,
                "horizon": horizon,
                "remove_top_winners": n,
                "removed_count": int(len(winners)),
                "remaining_count": raw["count"],
                "raw_mean": raw["mean"],
                "raw_median": raw["median"],
                "raw_win_rate": raw["win_rate"],
                "excess_mean": excess.get("mean"),
                "excess_median": excess.get("median"),
                "excess_win_rate": excess.get("win_rate"),
            }
        )
    return rows


def _extreme_rows(name: str, d: pd.DataFrame, horizon: int, side: str, n: int = 20) -> pd.DataFrame:
    ret_col = f"entry_fwd_ret_{horizon}d"
    excess_col = f"entry_excess_vs_csi1000_{horizon}d"
    ascending = side == "worst"
    cols = [
        "trade_date",
        "entry_date",
        "code",
        "name",
        "v4_rank",
        "v4_score",
        "g2_open_state",
        "pattern",
        "trigger_type",
        "entry_price",
        ret_col,
        excess_col,
    ]
    keep = [col for col in cols if col in d.columns]
    out = d.sort_values(ret_col, ascending=ascending).head(n)[keep].copy()
    out.insert(0, "candidate", name)
    out.insert(1, "side", side)
    out.insert(2, "horizon", horizon)
    return out


def run(input_path: Path, output_dir: Path) -> Dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    df = pd.read_parquet(input_path)
    overall: List[Dict[str, Any]] = []
    monthly: List[Dict[str, Any]] = []
    code_frames: List[pd.DataFrame] = []
    leave_top: List[Dict[str, Any]] = []
    extremes: List[pd.DataFrame] = []

    for name, pattern, state, trigger_type, horizon in DEFAULT_CANDIDATES:
        d = _filter_candidate(df, pattern, state, trigger_type)
        overall.append(_overall_row(name, d, horizon))
        monthly.extend(_monthly_rows(name, d, horizon))
        code_frames.append(_code_rows(name, d, horizon))
        leave_top.extend(_leave_top_rows(name, d, horizon))
        extremes.append(_extreme_rows(name, d, horizon, "best"))
        extremes.append(_extreme_rows(name, d, horizon, "worst"))

    overall_df = pd.DataFrame(overall)
    monthly_df = pd.DataFrame(monthly)
    code_df = pd.concat(code_frames, ignore_index=True) if code_frames else pd.DataFrame()
    leave_top_df = pd.DataFrame(leave_top)
    extreme_df = pd.concat(extremes, ignore_index=True) if extremes else pd.DataFrame()

    overall_df.to_csv(output_dir / "overall.csv", index=False, encoding="utf-8-sig")
    monthly_df.to_csv(output_dir / "monthly.csv", index=False, encoding="utf-8-sig")
    code_df.to_csv(output_dir / "code_concentration.csv", index=False, encoding="utf-8-sig")
    leave_top_df.to_csv(output_dir / "leave_top_winners.csv", index=False, encoding="utf-8-sig")
    extreme_df.to_csv(output_dir / "extreme_trades.csv", index=False, encoding="utf-8-sig")

    payload = {
        "schema_version": 1,
        "input": str(input_path),
        "candidates": [
            {"name": name, "pattern": pattern, "state": state, "trigger_type": trigger_type, "horizon": horizon}
            for name, pattern, state, trigger_type, horizon in DEFAULT_CANDIDATES
        ],
        "outputs": {
            "overall": "overall.csv",
            "monthly": "monthly.csv",
            "code_concentration": "code_concentration.csv",
            "leave_top_winners": "leave_top_winners.csv",
            "extreme_trades": "extreme_trades.csv",
        },
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    _write_findings(output_dir, overall_df, monthly_df, code_df, leave_top_df)
    return payload


def _write_findings(output_dir: Path, overall: pd.DataFrame, monthly: pd.DataFrame, code_df: pd.DataFrame, leave_top: pd.DataFrame) -> None:
    lines = ["# G2 30m Candidate Stability", ""]
    lines.extend(["## Overall", "", "| candidate | n | raw_mean | raw_median | win | excess_mean | excess_median | excess_win | codes |", "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"])
    for _, row in overall.iterrows():
        lines.append(
            f"| {row['candidate']} | {int(row['raw_count'])} | {row['raw_mean']:.2%} | {row['raw_median']:.2%} | {row['raw_win_rate']:.2%} | {row['excess_mean']:.2%} | {row['excess_median']:.2%} | {row['excess_win_rate']:.2%} | {int(row['unique_codes'])} |"
        )

    lines.extend(["", "## Monthly Pass Rate", "", "| candidate | months | positive_raw_months | positive_excess_months | worst_month_raw |", "| --- | ---: | ---: | ---: | ---: |"])
    for candidate, g in monthly.groupby("candidate", observed=True):
        valid = g[g["raw_count"] >= 3].copy()
        months = len(valid)
        positive_raw = int((valid["raw_mean"] > 0).sum())
        positive_excess = int((valid["excess_mean"] > 0).sum()) if "excess_mean" in valid.columns else 0
        worst = float(valid["raw_mean"].min()) if months else 0.0
        lines.append(f"| {candidate} | {months} | {positive_raw} | {positive_excess} | {worst:.2%} |")

    lines.extend(["", "## Top Code Concentration", "", "| candidate | top_code | top_name | trades | raw_sum | abs_share |", "| --- | --- | --- | ---: | ---: | ---: |"])
    for candidate, g in code_df.groupby("candidate", observed=True):
        top = g.sort_values("abs_raw_share", ascending=False).head(1)
        if top.empty:
            continue
        row = top.iloc[0]
        top10 = g.sort_values("abs_raw_share", ascending=False).head(10)
        top10_abs_share = float(top10["abs_raw_share"].sum())
        lines.append(
            f"| {candidate} | {row['code']} | {row['name']} | {int(row['trade_count'])} | {float(row['raw_sum']):.2%} | {float(row['abs_raw_share']):.2%} / top10 {top10_abs_share:.2%} |"
        )

    lines.extend(["", "## Leave Top Winners", "", "| candidate | remove | raw_mean | raw_median | win | excess_mean |", "| --- | ---: | ---: | ---: | ---: | ---: |"])
    for _, row in leave_top[leave_top["remove_top_winners"].isin([1, 5, 10])].iterrows():
        lines.append(
            f"| {row['candidate']} | {int(row['remove_top_winners'])} | {row['raw_mean']:.2%} | {row['raw_median']:.2%} | {row['raw_win_rate']:.2%} | {row['excess_mean']:.2%} |"
        )

    (output_dir / "findings.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze stability and concentration for G2 30m candidate cells.")
    parser.add_argument("--input", default=str(DEFAULT_INPUT))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    args = parser.parse_args()
    payload = run(Path(args.input), Path(args.output_dir))
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()

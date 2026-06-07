from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

DEFAULT_TIMING_DIR = REPO_ROOT / "reports" / "gen2_timing_research"
DEFAULT_EVENT_DATASET = REPO_ROOT / "reports" / "gen2_event_study" / "v4_event_dataset.parquet"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "reports" / "gen2_open_state_research"

INDEX_NAMES = {
    "999999.SH": "shanghai",
    "000300.SH": "csi300",
    "000905.SH": "csi500",
    "000852.SH": "csi1000",
    "399006.SZ": "chinext",
}

HORIZONS = (1, 2, 3, 5, 10, 20)


def _json_default(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
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


def _load_index_features(timing_dir: Path) -> pd.DataFrame:
    path = timing_dir / "index_features.csv"
    if not path.exists():
        raise FileNotFoundError(f"Missing index features: {path}")
    df = pd.read_csv(path)
    if df.empty:
        raise RuntimeError(f"Empty index features: {path}")
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
    return df


def _load_breadth(timing_dir: Path) -> pd.DataFrame:
    path = timing_dir / "market_breadth.csv"
    if not path.exists():
        raise FileNotFoundError(f"Missing market breadth: {path}")
    df = pd.read_csv(path)
    if df.empty:
        raise RuntimeError(f"Empty market breadth: {path}")
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
    return df


def _col(prefix: str, field: str) -> str:
    return f"{prefix}_{field}"


def _build_wide_index_features(index_df: pd.DataFrame, breadth_df: pd.DataFrame) -> pd.DataFrame:
    fields = ["close", "ret", "ma20", "ma60", "ma120", "ma20_slope5", "mom20", "above_ma20"]
    frames: List[pd.DataFrame] = []
    for code, prefix in INDEX_NAMES.items():
        part = index_df[index_df["code"] == code].copy()
        if part.empty:
            continue
        keep = ["trade_date"] + [field for field in fields if field in part.columns]
        part = part[keep].copy()
        rename = {field: _col(prefix, field) for field in keep if field != "trade_date"}
        frames.append(part.rename(columns=rename))
    if not frames:
        raise RuntimeError("No configured index features found.")
    wide = frames[0]
    for frame in frames[1:]:
        wide = wide.merge(frame, on="trade_date", how="outer")
    wide = wide.merge(breadth_df[["trade_date", "breadth_ma20", "stock_count"]], on="trade_date", how="left")
    wide = wide.sort_values("trade_date").reset_index(drop=True)
    return wide


def _bool_series(df: pd.DataFrame, column: str) -> pd.Series:
    if column not in df.columns:
        return pd.Series(False, index=df.index)
    return df[column].fillna(False).astype(bool)


def _num_series(df: pd.DataFrame, column: str) -> pd.Series:
    if column not in df.columns:
        return pd.Series(np.nan, index=df.index, dtype="float64")
    return pd.to_numeric(df[column], errors="coerce")


def _build_state_table(wide: pd.DataFrame, factor_mode: str) -> pd.DataFrame:
    d = wide.copy()
    breadth = _num_series(d, "breadth_ma20")
    active_above = _bool_series(d, "csi1000_above_ma20")
    active_slope_up = _num_series(d, "csi1000_ma20_slope5").fillna(0.0) > 0
    confirm_above = _bool_series(d, "csi500_above_ma20")
    growth_above = _bool_series(d, "chinext_above_ma20")
    risk_above = _bool_series(d, "shanghai_above_ma20")
    risk_slope_up = _num_series(d, "shanghai_ma20_slope5").fillna(0.0) > 0
    breadth_improve3 = breadth.diff().gt(0).rolling(3, min_periods=3).sum().eq(3)
    risk_ok = risk_above | risk_slope_up | breadth.ge(0.55)

    # Temporary proxy until the full GTJA/Alpha191 registry is available.
    # It keeps the state machine executable without turning factor research into parameter search.
    factor_risk_on_proxy = (
        _num_series(d, "csi1000_mom20").fillna(0.0).gt(0)
        & _num_series(d, "csi500_mom20").fillna(0.0).gt(0)
        & breadth.diff(5).fillna(0.0).ge(0)
    )
    if factor_mode == "off":
        factor_risk_on = pd.Series(False, index=d.index)
    elif factor_mode == "proxy":
        factor_risk_on = factor_risk_on_proxy
    else:
        raise ValueError(f"Unsupported factor_mode: {factor_mode}")

    off = ((~active_above) & breadth.lt(0.40)) | ((~active_above) & (~risk_above))
    normal = active_above & (active_slope_up | confirm_above) & breadth.ge(0.45) & risk_ok
    aggressive = normal & confirm_above & growth_above & breadth.ge(0.60) & factor_risk_on
    probe = (
        (active_above & breadth.ge(0.30) & breadth.lt(0.45))
        | (active_above & (~active_slope_up))
        | ((~active_above) & confirm_above & breadth_improve3)
    )

    state = np.select(
        [off, aggressive, normal, probe],
        ["OFF", "AGGRESSIVE", "NORMAL", "PROBE"],
        default="OFF",
    )
    d["g2_open_state"] = state
    d["target_exposure_min"] = d["g2_open_state"].map({"OFF": 0.0, "PROBE": 0.0, "NORMAL": 0.30, "AGGRESSIVE": 0.70})
    d["target_exposure_max"] = d["g2_open_state"].map({"OFF": 0.20, "PROBE": 0.30, "NORMAL": 0.70, "AGGRESSIVE": 0.90})
    d["active_index_above_ma20"] = active_above
    d["active_index_slope_up"] = active_slope_up
    d["confirm_index_above_ma20"] = confirm_above
    d["growth_index_above_ma20"] = growth_above
    d["risk_index_ok"] = risk_ok
    d["breadth_improve3"] = breadth_improve3.fillna(False)
    d["factor_risk_on"] = factor_risk_on.fillna(False)
    d["factor_mode"] = factor_mode
    return d


def _add_forward_index_returns(state_df: pd.DataFrame) -> pd.DataFrame:
    d = state_df.copy()
    for prefix in INDEX_NAMES.values():
        close_col = _col(prefix, "close")
        if close_col not in d.columns:
            continue
        close = _num_series(d, close_col)
        for horizon in HORIZONS:
            d[f"{prefix}_fwd_ret_{horizon}d"] = close.shift(-horizon) / close - 1.0
    return d


def _summary_stats(values: pd.Series) -> Dict[str, Any]:
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


def _summarize_index_forward(state_df: pd.DataFrame) -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []
    for state, group in state_df.groupby("g2_open_state", sort=False):
        for code, prefix in INDEX_NAMES.items():
            for horizon in HORIZONS:
                col = f"{prefix}_fwd_ret_{horizon}d"
                if col not in group.columns:
                    continue
                row = {
                    "sample": "index",
                    "state": state,
                    "code": code,
                    "index": prefix,
                    "horizon": horizon,
                }
                row.update(_summary_stats(group[col]))
                rows.append(row)
    return pd.DataFrame(rows)


def _summarize_v4_events(state_df: pd.DataFrame, event_dataset: Path) -> pd.DataFrame:
    if not event_dataset.exists():
        return pd.DataFrame()
    events = pd.read_parquet(event_dataset)
    if events.empty:
        return pd.DataFrame()
    events = events.copy()
    events["trade_date"] = pd.to_datetime(events["trade_date"]).dt.strftime("%Y-%m-%d")
    merge_cols = ["trade_date", "g2_open_state"]
    for horizon in HORIZONS:
        col = f"csi1000_fwd_ret_{horizon}d"
        if col in state_df.columns:
            merge_cols.append(col)
    events = events.merge(state_df[merge_cols], on="trade_date", how="left")

    rows: List[Dict[str, Any]] = []
    entry_pass = events["entry_pass"].fillna(False).astype(bool)
    groups = {
        "score_pool": events,
        "entry_all": events[entry_pass],
        "entry_top30": events[entry_pass & events["v4_rank"].le(30)],
        "entry_top10": events[entry_pass & events["v4_rank"].le(10)],
    }
    for group_name, group_df in groups.items():
        if group_df.empty:
            continue
        for state, group in group_df.groupby("g2_open_state", dropna=False):
            for horizon in HORIZONS:
                ret_col = f"fwd_ret_{horizon}d"
                if ret_col not in group.columns:
                    continue
                row = {
                    "sample": "v4_event",
                    "state": state if pd.notna(state) else "UNKNOWN",
                    "v4_group": group_name,
                    "horizon": horizon,
                    "metric": "raw_return",
                }
                row.update(_summary_stats(group[ret_col]))
                rows.append(row)

                idx_col = f"csi1000_fwd_ret_{horizon}d"
                if idx_col in group.columns:
                    excess = pd.to_numeric(group[ret_col], errors="coerce") - pd.to_numeric(group[idx_col], errors="coerce")
                    excess_row = {
                        "sample": "v4_event",
                        "state": state if pd.notna(state) else "UNKNOWN",
                        "v4_group": group_name,
                        "horizon": horizon,
                        "metric": "excess_vs_csi1000",
                    }
                    excess_row.update(_summary_stats(excess))
                    rows.append(excess_row)
    return pd.DataFrame(rows)


def _write_notes(output_dir: Path, payload: Dict[str, Any]) -> None:
    text = f"""# G2 Open State Research

Generated: {payload["generated_at"]}

## Purpose

Build a daily G2 open-state table and compare each state against:

- index forward returns
- V4 reference-pool event forward returns
- V4 event excess return versus CSI1000

This is not a portfolio backtest. It is an environment and timing diagnostic.

## Files

- `g2_open_state_daily.csv`
- `g2_open_state_index_forward_summary.csv`
- `g2_open_state_v4_forward_summary.csv`
- `summary.json`

## State Counts

{payload["state_counts_markdown"]}
"""
    (output_dir / "notes.md").write_text(text, encoding="utf-8")


def _state_counts_markdown(state_df: pd.DataFrame) -> str:
    counts = state_df["g2_open_state"].value_counts().rename_axis("state").reset_index(name="days")
    counts["ratio"] = counts["days"] / max(len(state_df), 1)
    lines = ["| state | days | ratio |", "| --- | ---: | ---: |"]
    for _, row in counts.iterrows():
        lines.append(f"| {row['state']} | {int(row['days'])} | {float(row['ratio']):.2%} |")
    return "\n".join(lines)


def run(timing_dir: Path, event_dataset: Path, output_dir: Path, factor_mode: str) -> Dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    index_df = _load_index_features(timing_dir)
    breadth_df = _load_breadth(timing_dir)
    wide = _build_wide_index_features(index_df, breadth_df)
    state_df = _add_forward_index_returns(_build_state_table(wide, factor_mode=factor_mode))
    index_summary = _summarize_index_forward(state_df)
    v4_summary = _summarize_v4_events(state_df, event_dataset)

    state_df.to_csv(output_dir / "g2_open_state_daily.csv", index=False, encoding="utf-8-sig")
    index_summary.to_csv(output_dir / "g2_open_state_index_forward_summary.csv", index=False, encoding="utf-8-sig")
    v4_summary.to_csv(output_dir / "g2_open_state_v4_forward_summary.csv", index=False, encoding="utf-8-sig")

    payload = {
        "schema_version": 1,
        "generated_at": pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S%z"),
        "timing_dir": str(timing_dir),
        "event_dataset": str(event_dataset),
        "event_dataset_exists": event_dataset.exists(),
        "factor_mode": factor_mode,
        "date_min": str(state_df["trade_date"].min()),
        "date_max": str(state_df["trade_date"].max()),
        "state_counts": state_df["g2_open_state"].value_counts().to_dict(),
        "state_counts_markdown": _state_counts_markdown(state_df),
        "outputs": {
            "state_daily": "g2_open_state_daily.csv",
            "index_forward_summary": "g2_open_state_index_forward_summary.csv",
            "v4_forward_summary": "g2_open_state_v4_forward_summary.csv",
            "notes": "notes.md",
        },
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    _write_notes(output_dir, payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Build G2 open-state table and forward-return summaries.")
    parser.add_argument("--timing-dir", default=str(DEFAULT_TIMING_DIR))
    parser.add_argument("--event-dataset", default=str(DEFAULT_EVENT_DATASET))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--factor-mode", choices=["proxy", "off"], default="proxy")
    args = parser.parse_args()
    payload = run(
        timing_dir=Path(args.timing_dir),
        event_dataset=Path(args.event_dataset),
        output_dir=Path(args.output_dir),
        factor_mode=str(args.factor_mode),
    )
    print(
        json.dumps(
            {
                "output_dir": str(args.output_dir),
                "date_min": payload["date_min"],
                "date_max": payload["date_max"],
                "state_counts": payload["state_counts"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

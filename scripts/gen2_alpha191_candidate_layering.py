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


DEFAULT_DIRECTIONS = {
    "Alpha150": "low",
    "Alpha070": "low",
    "Alpha095": "low",
    "Alpha132": "low",
    "Alpha042": "high",
    "Alpha097": "low",
    "Alpha100": "low",
    "Alpha055": "low",
    "Alpha144": "high",
    "Alpha059": "low",
}

COMPOSITES = {
    "core10": ["Alpha150", "Alpha070", "Alpha095", "Alpha132", "Alpha042", "Alpha097", "Alpha100", "Alpha055", "Alpha144", "Alpha059"],
    "main3": ["Alpha150", "Alpha095", "Alpha144"],
    "volume5": ["Alpha150", "Alpha070", "Alpha095", "Alpha132", "Alpha144"],
    "volume_corr": ["Alpha042"],
    "repair2": ["Alpha059", "Alpha055"],
}

SINGLE_FACTOR_SCORES = {fid: [fid] for fid in DEFAULT_DIRECTIONS}

SEGMENTS = [
    ("train_2024h2_2025q1", "2024-07-09", "2025-03-31"),
    ("validation_2025q2_q4", "2025-04-01", "2025-12-31"),
    ("blind_2026ytd", "2026-01-01", "2026-05-21"),
    ("full", "1900-01-01", "2100-01-01"),
]


def _parse_directions(text: str) -> dict[str, str]:
    if not text:
        return dict(DEFAULT_DIRECTIONS)
    if text.strip().startswith("{"):
        return {str(k): str(v) for k, v in json.loads(text).items()}
    out = dict(DEFAULT_DIRECTIONS)
    for item in text.split(","):
        if ":" not in item:
            continue
        k, v = item.split(":", 1)
        out[k.strip()] = v.strip()
    return out


def _train_percentile(values: pd.Series, train_values: pd.Series) -> pd.Series:
    train = pd.to_numeric(train_values, errors="coerce").dropna().sort_values().to_numpy(dtype=float)
    raw = pd.to_numeric(values, errors="coerce")
    if len(train) == 0:
        return pd.Series(np.nan, index=values.index)
    pct = np.searchsorted(train, raw.to_numpy(dtype=float), side="right") / float(len(train))
    out = pd.Series(pct, index=values.index, dtype=float)
    out.loc[raw.isna()] = np.nan
    return out


def _add_scores(d: pd.DataFrame, factors: list[str], directions: dict[str, str], train_start: str, train_end: str) -> pd.DataFrame:
    out = d.copy()
    train_mask = (out["date"] >= pd.Timestamp(train_start)) & (out["date"] <= pd.Timestamp(train_end))
    for fid in factors:
        if fid not in out.columns:
            continue
        raw = pd.to_numeric(out[fid], errors="coerce")
        oriented = raw if directions.get(fid, "high") == "high" else -raw
        out[f"{fid}_oriented"] = oriented
        out[f"{fid}_train_pct"] = _train_percentile(oriented, oriented[train_mask])

    score_groups = {**COMPOSITES, **SINGLE_FACTOR_SCORES}
    for name, members in score_groups.items():
        cols = [f"{fid}_train_pct" for fid in members if f"{fid}_train_pct" in out.columns]
        if cols:
            out[f"score_{name}"] = out[cols].mean(axis=1, skipna=True)
            out[f"score_{name}_valid_count"] = out[cols].notna().sum(axis=1)
    return out


def _bucketize(series: pd.Series, buckets: int) -> pd.Series:
    s = pd.to_numeric(series, errors="coerce")
    valid = s.dropna()
    if valid.nunique() < 2:
        return pd.Series(pd.NA, index=series.index)
    try:
        return pd.qcut(s, q=min(buckets, valid.nunique()), labels=False, duplicates="drop") + 1
    except ValueError:
        return pd.Series(pd.NA, index=series.index)


def _safe_mean(s: pd.Series) -> float | None:
    vals = pd.to_numeric(s, errors="coerce").dropna()
    return float(vals.mean()) if len(vals) else None


def _safe_rate(s: pd.Series) -> float | None:
    vals = s.dropna()
    return float(vals.astype(bool).mean()) if len(vals) else None


def _daily_ic(d: pd.DataFrame, score_col: str, target: str) -> dict[str, Any]:
    ics: list[float] = []
    for _, day in d.dropna(subset=[score_col, target]).groupby("entry_date"):
        if len(day) < 2 or day[score_col].nunique() < 2:
            continue
        ic = day[score_col].rank().corr(day[target].rank())
        if pd.notna(ic) and math.isfinite(float(ic)):
            ics.append(float(ic))
    if not ics:
        return {"ic_days": 0, "mean_ic": None, "ic_ir": None, "ic_positive_ratio": None}
    s = pd.Series(ics, dtype=float)
    std = float(s.std(ddof=1)) if len(s) > 1 else 0.0
    return {
        "ic_days": int(len(s)),
        "mean_ic": float(s.mean()),
        "ic_ir": float(s.mean() / std * math.sqrt(252)) if std > 0 else None,
        "ic_positive_ratio": float((s > 0).mean()),
    }


def _summarize_bucket(d: pd.DataFrame, score_col: str, target: str, buckets: int, segment: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    work = d.dropna(subset=[score_col]).copy()
    work["bucket"] = _bucketize(work[score_col], buckets)
    rows: list[dict[str, Any]] = []
    for bucket, g in work.dropna(subset=["bucket"]).groupby("bucket"):
        rows.append(
            {
                "segment": segment,
                "score": score_col.replace("score_", ""),
                "target": target,
                "bucket": int(bucket),
                "count": int(len(g)),
                "mean_return": _safe_mean(g[target]),
                "median_return": float(pd.to_numeric(g[target], errors="coerce").median()) if g[target].notna().any() else None,
                "positive_rate": _safe_rate(pd.to_numeric(g[target], errors="coerce") > 0),
                "outcome_good_rate": _safe_rate(g["outcome_good"]) if "outcome_good" in g else None,
                "outcome_bad_rate": _safe_rate(g["outcome_bad"]) if "outcome_bad" in g else None,
                "stop5_touch_rate": _safe_rate(g["stop5_touch_30m"]) if "stop5_touch_30m" in g else None,
            }
        )
    top = work[work["bucket"] == work["bucket"].max()]
    bottom = work[work["bucket"] == work["bucket"].min()]
    spread = None
    if not top.empty and not bottom.empty:
        spread = (_safe_mean(top[target]) or 0.0) - (_safe_mean(bottom[target]) or 0.0)
    summary = {
        "segment": segment,
        "score": score_col.replace("score_", ""),
        "target": target,
        "count": int(len(work)),
        "buckets": int(work["bucket"].nunique(dropna=True)),
        "top_bottom_spread": spread,
        **_daily_ic(work, score_col, target),
    }
    return rows, summary


def run(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    source = pd.read_parquet(args.source)
    values = pd.read_parquet(args.values)
    source["date"] = pd.to_datetime(source["entry_date"], errors="coerce")
    source["code6"] = source["code"].astype(str).str[:6]
    values["date"] = pd.to_datetime(values["date"], errors="coerce")
    values["code6"] = values["code6"].astype(str).str[:6]
    d = source.merge(values, on=["date", "code6"], how="left", suffixes=("", "_alpha"))
    factors = [x.strip() for x in str(args.factors).split(",") if x.strip()]
    directions = _parse_directions(str(args.directions or ""))
    d = _add_scores(d, factors, directions, str(args.train_start), str(args.train_end))
    d.to_parquet(output_dir / "candidate_alpha191_scored.parquet", index=False)
    d.to_csv(output_dir / "candidate_alpha191_scored.csv", index=False, encoding="utf-8-sig")

    bucket_rows: list[dict[str, Any]] = []
    summary_rows: list[dict[str, Any]] = []
    targets = [x.strip() for x in str(args.targets).split(",") if x.strip()]
    score_groups = {**COMPOSITES, **SINGLE_FACTOR_SCORES}
    score_cols = [
        f"score_{name}"
        for name in score_groups
        if f"score_{name}" in d.columns
    ]
    for segment, start, end in SEGMENTS:
        seg = d[(d["date"] >= pd.Timestamp(start)) & (d["date"] <= pd.Timestamp(end))].copy()
        if seg.empty:
            continue
        for score_col in score_cols:
            for target in targets:
                if target not in seg.columns:
                    continue
                rows, summary = _summarize_bucket(seg, score_col, target, int(args.buckets), segment)
                bucket_rows.extend(rows)
                summary_rows.append(summary)

    buckets = pd.DataFrame(bucket_rows)
    summary = pd.DataFrame(summary_rows)
    if not summary.empty:
        summary = summary.sort_values(["segment", "target", "top_bottom_spread"], ascending=[True, True, False])
    buckets.to_csv(output_dir / "candidate_alpha191_bucket_returns.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(output_dir / "candidate_alpha191_layering_summary.csv", index=False, encoding="utf-8-sig")
    payload = {
        "schema_version": 1,
        "source": str(args.source),
        "values": str(args.values),
        "factors": factors,
        "directions": directions,
        "outputs": {
            "scored": "candidate_alpha191_scored.csv",
            "buckets": "candidate_alpha191_bucket_returns.csv",
            "summary": "candidate_alpha191_layering_summary.csv",
        },
        "top_summary": summary.head(30).where(pd.notna(summary.head(30)), None).to_dict("records") if not summary.empty else [],
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Layer G2 candidates by Alpha191 T-1 scores.")
    parser.add_argument("--source", default=str(ROOT / "reports" / "gen2_alpha191_overlay_candidate_train_dirs" / "sources" / "risk_cool_base.parquet"))
    parser.add_argument("--values", default=str(ROOT / "reports" / "gen2_alpha191_candidate_core10_t1" / "alpha191_core10_t1_signal_values.parquet"))
    parser.add_argument("--output-dir", default=str(ROOT / "reports" / "gen2_alpha191_candidate_core10_t1"))
    parser.add_argument("--factors", default=",".join(DEFAULT_DIRECTIONS.keys()))
    parser.add_argument("--directions", default="")
    parser.add_argument("--train-start", default="2024-07-09")
    parser.add_argument("--train-end", default="2025-03-31")
    parser.add_argument("--targets", default="outcome_fwd_ret_3d,outcome_fwd_ret_5d,outcome_fwd_ret_10d,outcome_fwd_ret_20d")
    parser.add_argument("--buckets", type=int, default=3)
    print(json.dumps(run(parser.parse_args()), ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()

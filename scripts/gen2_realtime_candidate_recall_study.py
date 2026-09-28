from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen2_backtest_open_v1_portfolio import _json_default, _pct  # noqa: E402
from scripts.gen2_filter_signals_by_intraday_normal import DEFAULT_STATE_DAILY, _build_intraday_state, _filter_with_state  # noqa: E402
from scripts.gen2_runtime_dates import add_end_date_argument, resolve_end_date  # noqa: E402
from scripts.gen2_test_30m_fractal_restart import DEFAULT_EVENT_DATASET, _find_fractal_triggers, _load_minute_bars, _load_trade_dates  # noqa: E402


DEFAULT_TARGET = (
    ROOT
    / "reports"
    / "gen2_intraday_normal_signal_filters_tday_context"
    / "signals_intraday_normal_30m_before_confirm.parquet"
)
DEFAULT_OUTPUT_DIR = ROOT / "reports" / "gen2_realtime_candidate_recall_study"

POOL_RULES = {
    "d1_entry_rank50": lambda d: d["entry_pass"].fillna(False).astype(bool) & pd.to_numeric(d["v4_rank"], errors="coerce").le(50),
    "d1_entry_rank100": lambda d: d["entry_pass"].fillna(False).astype(bool) & pd.to_numeric(d["v4_rank"], errors="coerce").le(100),
    "d1_score_rank150": lambda d: d["in_score_pool"].fillna(False).astype(bool) & pd.to_numeric(d["v4_rank"], errors="coerce").le(150),
    "d1_rank200": lambda d: pd.to_numeric(d["v4_rank"], errors="coerce").le(200),
}


def _load_target(path: Path, start_date: str, end_date: str) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Target file not found: {path}")
    d = pd.read_parquet(path)
    if d.empty:
        return pd.DataFrame(
            columns=[
                "entry_date",
                "confirm_datetime",
                "pattern",
                "trigger_type",
                "g2_open_state",
                "code",
                "target_key",
            ]
        )
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    d["confirm_datetime"] = pd.to_datetime(d["confirm_datetime"], errors="coerce")
    d = d[(d["entry_date"] >= start_date) & (d["entry_date"] <= end_date)].copy()
    d = d[
        d["pattern"].eq("pullback_restart_rank100")
        & d["trigger_type"].eq("bottom_fractal_break_high_vol")
        & d["g2_open_state"].eq("NORMAL")
    ].copy()
    d["target_key"] = d["code"].astype(str) + "|" + d["confirm_datetime"].dt.strftime("%Y-%m-%d %H:%M:%S")
    return d.dropna(subset=["entry_date", "code", "confirm_datetime"]).reset_index(drop=True)


def _load_events(path: Path, start_date: str, end_date: str) -> pd.DataFrame:
    d = pd.read_parquet(path)
    if d.empty:
        return pd.DataFrame(columns=["trade_date"])
    d["trade_date"] = pd.to_datetime(d["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    d = d[(d["trade_date"] >= start_date) & (d["trade_date"] <= end_date)].copy()
    for col in ["v4_rank", "v4_score", "close", "mom5", "mom10", "mom20", "vol_ratio", "vol10", "amt20"]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    return d.reset_index(drop=True)


def _build_contexts(events: pd.DataFrame, target_dates: list[str], trade_dates: list[str], pool: str) -> pd.DataFrame:
    idx = {date: i for i, date in enumerate(trade_dates)}
    prev_map = {trade_dates[i]: trade_dates[i - 1] for i in range(1, len(trade_dates))}
    prev_dates = {prev_map[date] for date in target_dates if date in prev_map}
    base = events[events["trade_date"].isin(prev_dates)].copy()
    mask = POOL_RULES[pool](base)
    base = base[mask.fillna(False)].copy()
    if base.empty:
        return base
    next_map = {v: k for k, v in prev_map.items()}
    base["entry_date"] = base["trade_date"].map(next_map)
    base = base[base["entry_date"].isin(target_dates)].copy()
    base["entry_start_date"] = base["entry_date"]
    base["search_end_date"] = base["entry_date"]
    base["trade_idx"] = base["trade_date"].map(idx)
    base["entry_start_idx"] = base["entry_date"].map(idx)
    base["search_end_idx"] = base["entry_date"].map(idx)
    base["ctx_realtime_pool"] = True
    base["g2_open_state"] = "D1_CONTEXT"
    return base.reset_index(drop=True)


def _attach_realtime_features(candidates: pd.DataFrame, target_keys: set[str]) -> pd.DataFrame:
    if candidates.empty:
        return candidates
    d = candidates.copy()
    d["confirm_datetime"] = pd.to_datetime(d["confirm_datetime"], errors="coerce")
    d["candidate_key"] = d["code"].astype(str) + "|" + d["confirm_datetime"].dt.strftime("%Y-%m-%d %H:%M:%S")
    d["is_same_day_target"] = d["candidate_key"].isin(target_keys)
    prev_close = pd.to_numeric(d.get("close"), errors="coerce")
    entry_price = pd.to_numeric(d.get("entry_price"), errors="coerce")
    d["rt_return_from_d1_close"] = entry_price / prev_close - 1.0
    d["rt_confirm_vs_ma5"] = entry_price / pd.to_numeric(d.get("confirm_ma5"), errors="coerce") - 1.0
    d["rt_confirm_vs_ma10"] = entry_price / pd.to_numeric(d.get("confirm_ma10"), errors="coerce") - 1.0
    d["rt_fractal_rebound"] = entry_price / pd.to_numeric(d.get("fractal_low"), errors="coerce") - 1.0
    d["rt_30m_amount_ratio"] = pd.to_numeric(d.get("confirm_amount"), errors="coerce") / pd.to_numeric(d.get("confirm_amount_ma5_prev"), errors="coerce")
    d["rt_confirm_hour"] = d["confirm_datetime"].dt.hour + d["confirm_datetime"].dt.minute / 60.0
    return d.replace([np.inf, -np.inf], np.nan)


def _make_candidates(events: pd.DataFrame, target: pd.DataFrame, trade_dates: list[str], state_daily: Path, pool: str) -> pd.DataFrame:
    target_dates = sorted(target["entry_date"].dropna().astype(str).unique().tolist())
    if not target_dates or not trade_dates:
        return pd.DataFrame()
    contexts = _build_contexts(events, target_dates, trade_dates, pool)
    if contexts.empty:
        return contexts
    codes = contexts["code"].dropna().astype(str).unique().tolist()
    start_date = min(set(contexts["trade_date"].astype(str)))
    end_date = max(target_dates)
    minute_bars = _load_minute_bars(codes, start_date, end_date)
    triggers = _find_fractal_triggers(contexts, minute_bars)
    if triggers.empty:
        return triggers
    triggers = triggers[triggers["trigger_type"].eq("bottom_fractal_break_high_vol")].copy()
    triggers["entry_date"] = pd.to_datetime(triggers["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    triggers = triggers[triggers["entry_date"].isin(target_dates)].copy()
    state = _build_intraday_state(triggers, state_daily, 30)
    filtered = _filter_with_state(triggers, state, "before_confirm")
    filtered = filtered[filtered["trigger_type"].eq("bottom_fractal_break_high_vol")].copy()
    filtered["realtime_pool"] = pool
    return _attach_realtime_features(filtered, set(target["target_key"]))


def _summarize(candidates: pd.DataFrame, target: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    target_keys = set(target["target_key"])
    for pool, g in candidates.groupby("realtime_pool", dropna=False):
        keys = set(g["candidate_key"].dropna().astype(str))
        hits = keys & target_keys
        rows.append(
            {
                "realtime_pool": pool,
                "candidates": int(len(keys)),
                "target_hits": int(len(hits)),
                "target_total": int(len(target_keys)),
                "recall": len(hits) / len(target_keys) if target_keys else np.nan,
                "precision": len(hits) / len(keys) if keys else np.nan,
                "unique_codes": int(g["code"].nunique()),
                "signal_days": int(g["entry_date"].nunique()),
            }
        )
    return pd.DataFrame(rows).sort_values(["recall", "precision"], ascending=[False, False])


def _feature_contrast(candidates: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    features = [
        "v4_rank",
        "v4_score",
        "mom5",
        "mom10",
        "mom20",
        "vol_ratio",
        "vol10",
        "rt_return_from_d1_close",
        "rt_confirm_vs_ma5",
        "rt_confirm_vs_ma10",
        "rt_fractal_rebound",
        "rt_30m_amount_ratio",
        "rt_confirm_hour",
    ]
    for pool, g in candidates.groupby("realtime_pool", dropna=False):
        pos = g[g["is_same_day_target"]]
        neg = g[~g["is_same_day_target"]]
        for col in features:
            p = pd.to_numeric(pos.get(col), errors="coerce").dropna()
            n = pd.to_numeric(neg.get(col), errors="coerce").dropna()
            if p.empty or n.empty:
                continue
            rows.append(
                {
                    "realtime_pool": pool,
                    "feature": col,
                    "target_mean": float(p.mean()),
                    "other_mean": float(n.mean()),
                    "diff": float(p.mean() - n.mean()),
                    "target_median": float(p.median()),
                    "other_median": float(n.median()),
                }
            )
    return pd.DataFrame(rows).sort_values(["realtime_pool", "diff"], ascending=[True, False])


def _write_report(output_dir: Path, summary: pd.DataFrame, contrast: pd.DataFrame) -> None:
    lines = [
        "# G2 Realtime Candidate Recall Study",
        "",
        "Target is the 349 same-day-context signal set. Candidate pools use only D-1 V4/event fields plus target-day 30m confirmation and intraday NORMAL before confirm.",
        "",
        "## Pool Recall",
        "",
        "| pool | candidates | target_hits | recall | precision | signal_days |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for _, row in summary.iterrows():
        lines.append(
            f"| {row['realtime_pool']} | {int(row['candidates'])} | {int(row['target_hits'])} | "
            f"{_pct(row['recall'])} | {_pct(row['precision'])} | {int(row['signal_days'])} |"
        )
    lines.extend(["", "## Strongest Mean Differences", ""])
    lines.append("| pool | feature | target_mean | other_mean | diff |")
    lines.append("| --- | --- | ---: | ---: | ---: |")
    for _, row in contrast.reindex(contrast["diff"].abs().sort_values(ascending=False).index).head(30).iterrows():
        lines.append(
            f"| {row['realtime_pool']} | {row['feature']} | {row['target_mean']:.4f} | {row['other_mean']:.4f} | {row['diff']:.4f} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "A high-recall pool is useful for the next stage only if it can be combined with simple realtime filters without collapsing recall.",
            "Low precision is acceptable at this stage because portfolio rules and preference tiers can filter later.",
        ]
    )
    (output_dir / "recall_study_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _target_d1_profile(target: pd.DataFrame, events: pd.DataFrame, trade_dates: list[str]) -> pd.DataFrame:
    idx = {date: i for i, date in enumerate(trade_dates)}
    prev_map = {trade_dates[i]: trade_dates[i - 1] for i in range(1, len(trade_dates))}
    t = target.copy()
    if t.empty:
        return pd.DataFrame(
            columns=[
                "target_key",
                "code",
                "d1_rank_bucket",
                "d1_entry_pass",
                "d1_in_score_pool",
                "d1_v4_rank",
                "d1_v4_score",
                "d1_mom5",
                "d1_mom10",
                "d1_mom20",
                "d1_vol_ratio",
            ]
        )
    t["d1_date"] = t["entry_date"].map(prev_map)
    if events.empty:
        t["target_key"] = t["code"].astype(str) + "|" + pd.to_datetime(t["confirm_datetime"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")
        t["d1_rank_bucket"] = pd.Series([np.nan] * len(t), dtype="category")
        for c in ["d1_entry_pass", "d1_in_score_pool", "d1_v4_rank", "d1_v4_score", "d1_mom5", "d1_mom10", "d1_mom20", "d1_vol_ratio"]:
            t[c] = pd.Series([np.nan] * len(t))
        return t[["target_key", "code", "d1_rank_bucket", "d1_entry_pass", "d1_in_score_pool", "d1_v4_rank", "d1_v4_score", "d1_mom5", "d1_mom10", "d1_mom20", "d1_vol_ratio"]]
    d1_cols = [
        "trade_date",
        "code",
        "v4_rank",
        "v4_score",
        "entry_pass",
        "in_score_pool",
        "rank_change_status",
        "pool_streak",
        "close",
        "mom5",
        "mom10",
        "mom20",
        "vol_ratio",
        "vol10",
        "amt20",
    ]
    d1 = events[[col for col in d1_cols if col in events.columns]].copy()
    if "trade_date" not in d1.columns or "code" not in d1.columns:
        t["target_key"] = t["code"].astype(str) + "|" + pd.to_datetime(t["confirm_datetime"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")
        t["d1_rank_bucket"] = pd.Series([np.nan] * len(t), dtype="category")
        for c in ["d1_entry_pass", "d1_in_score_pool", "d1_v4_rank", "d1_v4_score", "d1_mom5", "d1_mom10", "d1_mom20", "d1_vol_ratio"]:
            t[c] = pd.Series([np.nan] * len(t))
        return t[["target_key", "code", "d1_rank_bucket", "d1_entry_pass", "d1_in_score_pool", "d1_v4_rank", "d1_v4_score", "d1_mom5", "d1_mom10", "d1_mom20", "d1_vol_ratio"]]
    merged = t.merge(
        d1.rename(columns={col: f"d1_{col}" for col in d1.columns if col not in {"code"}}),
        left_on=["code", "d1_date"],
        right_on=["code", "d1_trade_date"],
        how="left",
    )
    merged["target_key"] = merged["code"].astype(str) + "|" + pd.to_datetime(merged["confirm_datetime"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")
    merged["d1_rank_bucket"] = pd.cut(
        pd.to_numeric(merged.get("d1_v4_rank"), errors="coerce"),
        bins=[0, 50, 100, 150, 200, 300, 500, 9999],
        labels=["<=50", "51-100", "101-150", "151-200", "201-300", "301-500", ">500"],
        include_lowest=True,
    )
    return merged


def _miss_profile(target_profile: pd.DataFrame, candidates: pd.DataFrame, output_dir: Path) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for pool, g in candidates.groupby("realtime_pool", dropna=False):
        hit_keys = set(g.loc[g["is_same_day_target"], "candidate_key"].dropna().astype(str))
        p = target_profile.copy()
        p["hit_by_pool"] = p["target_key"].isin(hit_keys)
        for hit, group in p.groupby("hit_by_pool", dropna=False):
            rows.append(
                {
                    "realtime_pool": pool,
                    "group": "hit" if hit else "miss",
                    "targets": int(len(group)),
                    "d1_entry_pass_rate": float(group.get("d1_entry_pass", pd.Series(dtype=bool)).fillna(False).astype(bool).mean()),
                    "d1_in_score_pool_rate": float(group.get("d1_in_score_pool", pd.Series(dtype=bool)).fillna(False).astype(bool).mean()),
                    "d1_rank_median": float(pd.to_numeric(group.get("d1_v4_rank"), errors="coerce").median()),
                    "d1_score_mean": float(pd.to_numeric(group.get("d1_v4_score"), errors="coerce").mean()),
                    "d1_mom5_mean": float(pd.to_numeric(group.get("d1_mom5"), errors="coerce").mean()),
                    "d1_mom10_mean": float(pd.to_numeric(group.get("d1_mom10"), errors="coerce").mean()),
                    "d1_mom20_mean": float(pd.to_numeric(group.get("d1_mom20"), errors="coerce").mean()),
                    "d1_vol_ratio_mean": float(pd.to_numeric(group.get("d1_vol_ratio"), errors="coerce").mean()),
                }
            )
    profile = pd.DataFrame(rows)
    bucket = (
        target_profile.groupby("d1_rank_bucket", dropna=False, observed=False)
        .agg(targets=("target_key", "nunique"))
        .reset_index()
    )
    bucket.to_csv(output_dir / "target_d1_rank_bucket.csv", index=False, encoding="utf-8-sig")
    target_profile.to_csv(output_dir / "target_d1_profile.csv", index=False, encoding="utf-8-sig")
    return profile


def run(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    target = _load_target(Path(args.target), str(args.start_date), str(args.end_date))
    if target.empty:
        summary = pd.DataFrame(columns=["realtime_pool", "candidates", "target_hits", "target_total", "recall", "precision", "unique_codes", "signal_days"])
        contrast = pd.DataFrame(columns=["realtime_pool", "feature", "target_mean", "other_mean", "diff", "target_median", "other_median"])
        miss_profile = pd.DataFrame(columns=["realtime_pool", "group", "targets", "d1_entry_pass_rate", "d1_in_score_pool_rate", "d1_rank_median", "d1_score_mean", "d1_mom5_mean", "d1_mom10_mean", "d1_mom20_mean", "d1_vol_ratio_mean"])
        candidates = pd.DataFrame()
        trade_dates = _load_trade_dates(str(args.start_date), str(args.end_date))
        target_profile = _target_d1_profile(target, pd.DataFrame(), trade_dates)
        _write_report(output_dir, summary, contrast)
        summary.to_csv(output_dir / "pool_recall_summary.csv", index=False, encoding="utf-8-sig")
        contrast.to_csv(output_dir / "feature_contrast.csv", index=False, encoding="utf-8-sig")
        miss_profile.to_csv(output_dir / "miss_profile.csv", index=False, encoding="utf-8-sig")
        target_profile.to_csv(output_dir / "target_d1_profile.csv", index=False, encoding="utf-8-sig")
        candidates.to_csv(output_dir / "realtime_candidates.csv", index=False, encoding="utf-8-sig")
        candidates.to_parquet(output_dir / "realtime_candidates.parquet", index=False)
        payload = {
            "schema_version": 1,
            "target": str(Path(args.target)),
            "event_dataset": str(Path(args.event_dataset)),
            "state_daily": str(Path(args.state_daily)),
            "start_date": str(args.start_date),
            "end_date": str(args.end_date),
            "target_signals": int(target["target_key"].nunique()) if "target_key" in target.columns else 0,
            "pools": [x.strip() for x in str(args.pools).split(",") if x.strip()],
            "summary": [],
            "outputs": {
                "report": "recall_study_report.md",
                "candidates": "realtime_candidates.parquet",
                "pool_recall": "pool_recall_summary.csv",
                "feature_contrast": "feature_contrast.csv",
            },
        }
        (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
        return payload
    trade_dates = _load_trade_dates(str(args.start_date), str(args.end_date))
    if not trade_dates:
        raise ValueError("No trade dates found for requested window.")
    in_scope_target_dates = [x for x in target["entry_date"].unique() if x in trade_dates]
    if not in_scope_target_dates:
        min_prev_idx = 0
    else:
        min_prev_idx = max(0, min(trade_dates.index(x) for x in in_scope_target_dates) - 1)
    events = _load_events(Path(args.event_dataset), trade_dates[min_prev_idx], str(args.end_date))

    parts: list[pd.DataFrame] = []
    pools = [x.strip() for x in str(args.pools).split(",") if x.strip()]
    for pool in pools:
        if pool not in POOL_RULES:
            raise ValueError(f"Unknown pool: {pool}")
        part = _make_candidates(events, target, trade_dates, Path(args.state_daily), pool)
        if not part.empty:
            parts.append(part)
    candidates = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    summary = _summarize(candidates, target) if not candidates.empty else pd.DataFrame()
    contrast = _feature_contrast(candidates) if not candidates.empty else pd.DataFrame()
    target_profile = _target_d1_profile(target, events, trade_dates)
    miss_profile = _miss_profile(target_profile, candidates, output_dir) if not candidates.empty else pd.DataFrame()

    candidates.to_csv(output_dir / "realtime_candidates.csv", index=False, encoding="utf-8-sig")
    candidates.to_parquet(output_dir / "realtime_candidates.parquet", index=False)
    summary.to_csv(output_dir / "pool_recall_summary.csv", index=False, encoding="utf-8-sig")
    contrast.to_csv(output_dir / "feature_contrast.csv", index=False, encoding="utf-8-sig")
    miss_profile.to_csv(output_dir / "miss_profile.csv", index=False, encoding="utf-8-sig")
    _write_report(output_dir, summary, contrast)
    payload = {
        "schema_version": 1,
        "target": str(Path(args.target)),
        "event_dataset": str(Path(args.event_dataset)),
        "state_daily": str(Path(args.state_daily)),
        "start_date": str(args.start_date),
        "end_date": str(args.end_date),
        "target_signals": int(target["target_key"].nunique()),
        "pools": pools,
        "summary": summary.where(pd.notna(summary), None).to_dict("records"),
        "outputs": {
            "report": "recall_study_report.md",
            "candidates": "realtime_candidates.parquet",
            "pool_recall": "pool_recall_summary.csv",
            "feature_contrast": "feature_contrast.csv",
        },
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Study whether D-1 realtime-visible candidate pools can recall same-day G2 target signals.")
    parser.add_argument("--target", default=str(DEFAULT_TARGET))
    parser.add_argument("--event-dataset", default=str(DEFAULT_EVENT_DATASET))
    parser.add_argument("--state-daily", default=str(DEFAULT_STATE_DAILY))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--start-date", default="2024-07-09")
    add_end_date_argument(parser)
    parser.add_argument("--pools", default="d1_entry_rank50,d1_entry_rank100,d1_score_rank150,d1_rank200")
    args = parser.parse_args()
    args.end_date = resolve_end_date(args.end_date)
    print(json.dumps(run(args), ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()

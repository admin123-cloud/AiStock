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

from api.gen2_factor import _add_factor, _estimate_prewarm_days, _load_daily_ohlcv, build_gen2_factor_registry  # noqa: E402
from scripts.gen2_backtest_open_v1_portfolio import _json_default  # noqa: E402
from scripts.gen2_backtest_risk_cool_dynamic_circuit import (  # noqa: E402
    DEFAULT_CANDIDATES,
    DEFAULT_SHARE_CAP_CACHE,
    _build_signal_source,
    _run_dynamic,
)


DEFAULT_OUTPUT_DIR = ROOT / "reports" / "gen2_alpha191_overlay"
DEFAULT_FACTORS = [
    "Alpha150",
    "Alpha070",
    "Alpha095",
    "Alpha132",
    "Alpha144",
    "Alpha062",
    "Alpha139",
    "Alpha097",
    "Alpha100",
    "Alpha083",
    "Alpha102",
]


COMPOSITES = {
    "alpha191_offensive_low": ["Alpha150", "Alpha070", "Alpha095", "Alpha132"],
    "alpha191_edge_stable": ["Alpha062", "Alpha139", "Alpha097", "Alpha100", "Alpha083", "Alpha102"],
    "alpha191_balanced": ["Alpha150", "Alpha070", "Alpha095", "Alpha144", "Alpha062", "Alpha139", "Alpha097"],
}


def _factor_lookup() -> dict[str, dict[str, Any]]:
    factors = build_gen2_factor_registry().get("factors") or []
    return {str(item.get("id")): item for item in factors}


def _read_factor_directions(path: Path, factor_ids: list[str]) -> dict[str, str]:
    if not path.exists():
        return {}
    d = pd.read_csv(path)
    out: dict[str, str] = {}
    for row in d.itertuples(index=False):
        fid = str(getattr(row, "factor_id", ""))
        if fid in factor_ids:
            direction = str(getattr(row, "direction", "") or "").lower()
            if direction in {"high", "low"}:
                out[fid] = direction
    return out


def _candidate_directions(
    base: pd.DataFrame,
    values: pd.DataFrame,
    factor_ids: list[str],
    target: str,
    start_date: str,
    end_date: str,
) -> dict[str, str]:
    d = base.copy()
    d["date"] = pd.to_datetime(d["entry_date"], errors="coerce")
    d["code6"] = d["code"].map(_canonical_code)
    d = d.merge(values, on=["date", "code6"], how="left")
    d = d[(d["date"] >= pd.Timestamp(start_date)) & (d["date"] <= pd.Timestamp(end_date))].copy()
    d[target] = pd.to_numeric(d[target], errors="coerce")
    directions: dict[str, str] = {}
    for fid in factor_ids:
        if fid not in d.columns:
            continue
        ics: list[float] = []
        for _, day in d.dropna(subset=[fid, target]).groupby("entry_date"):
            if len(day) < 2 or day[fid].nunique() < 2:
                continue
            ic = day[fid].rank().corr(day[target].rank())
            if pd.notna(ic) and math.isfinite(float(ic)):
                ics.append(float(ic))
        if ics:
            directions[fid] = "high" if float(np.mean(ics)) >= 0 else "low"
    return directions


def _canonical_code(value: Any) -> str:
    return str(value or "").strip()[:6]


def _metric_from_curve(curve: pd.DataFrame) -> dict[str, Any]:
    if curve.empty or "strategy_equity" not in curve.columns:
        return {"daily_sharpe": None}
    equity = pd.to_numeric(curve["strategy_equity"], errors="coerce").dropna()
    rets = equity.pct_change().dropna()
    if len(rets) < 2:
        sharpe = None
    else:
        std = float(rets.std(ddof=1))
        sharpe = None if std <= 0 or not math.isfinite(std) else float(rets.mean() / std * math.sqrt(252))
    return {
        "daily_sharpe": sharpe,
        "daily_mean": float(rets.mean()) if len(rets) else None,
        "daily_std": float(rets.std(ddof=1)) if len(rets) > 1 else None,
    }


def _segment_metrics(curve: pd.DataFrame) -> list[dict[str, Any]]:
    if curve.empty:
        return []
    d = curve.copy()
    d["date"] = pd.to_datetime(d["date"], errors="coerce")
    rows: list[dict[str, Any]] = []
    for name, start, end in [
        ("train_2024h2_2025q1", "2024-07-09", "2025-03-31"),
        ("validation_2025q2_q4", "2025-04-01", "2025-12-31"),
        ("blind_2026ytd", "2026-01-01", "2026-05-21"),
    ]:
        g = d[(d["date"] >= pd.Timestamp(start)) & (d["date"] <= pd.Timestamp(end))].copy()
        if g.empty:
            continue
        first = float(g["strategy_equity"].iloc[0])
        last = float(g["strategy_equity"].iloc[-1])
        metrics = _metric_from_curve(g)
        rows.append(
            {
                "segment": name,
                "return": last / first - 1.0 if first > 0 else None,
                "max_drawdown": float(g["drawdown"].min()) if "drawdown" in g.columns else None,
                "daily_sharpe": metrics["daily_sharpe"],
                "days": int(len(g)),
            }
        )
    return rows


def _load_alpha_values(source: pd.DataFrame, factor_ids: list[str], start_date: str, end_date: str) -> pd.DataFrame:
    metas = _factor_lookup()
    selected = [metas[fid] for fid in factor_ids if fid in metas and metas[fid].get("backend_status") == "implemented"]
    if not selected:
        raise RuntimeError("No implemented Alpha191 factors selected.")
    prewarm = max(_estimate_prewarm_days(meta) for meta in selected)
    daily = _load_daily_ohlcv(start_date, end_date, horizon=10, prewarm_days=prewarm)
    if daily.empty:
        raise RuntimeError("Daily OHLCV data is empty.")

    needed = source[["entry_date", "code"]].copy()
    needed["date"] = pd.to_datetime(needed["entry_date"], errors="coerce")
    needed["code6"] = needed["code"].map(_canonical_code)
    needed = needed.dropna(subset=["date", "code6"]).drop_duplicates(["date", "code6"])

    values = needed.copy()
    for meta in selected:
        fid = str(meta.get("id"))
        print(f"computing {fid}", flush=True)
        factored = _add_factor(daily, meta)
        slim = factored[["date", "code", "factor_value"]].rename(columns={"code": "code6", "factor_value": fid})
        values = values.merge(slim, on=["date", "code6"], how="left")
    return values.drop(columns=["entry_date", "code"]).copy()


def _score_signal_source(
    base: pd.DataFrame,
    values: pd.DataFrame,
    directions: dict[str, str],
    factors: list[str],
    mode: str,
    keep_top_frac: float | None,
) -> pd.DataFrame:
    d = base.copy()
    d["date"] = pd.to_datetime(d["entry_date"], errors="coerce")
    d["code6"] = d["code"].map(_canonical_code)
    d = d.merge(values, on=["date", "code6"], how="left")

    score_cols: list[str] = []
    for fid in factors:
        if fid not in d.columns:
            continue
        direction = directions.get(fid, "high")
        raw = pd.to_numeric(d[fid], errors="coerce")
        oriented = -raw if direction == "low" else raw
        col = f"{fid}_oriented_rank"
        d[col] = oriented.groupby(d["entry_date"]).rank(method="average", pct=True)
        score_cols.append(col)
    if not score_cols:
        raise RuntimeError(f"No usable score columns for {mode}.")

    d["alpha191_score"] = d[score_cols].mean(axis=1, skipna=True)
    d["alpha191_score_valid_count"] = d[score_cols].notna().sum(axis=1)
    d["alpha191_rank_in_day"] = d.groupby("entry_date")["alpha191_score"].rank(method="first", ascending=False)
    d["alpha191_candidates_in_day"] = d.groupby("entry_date")["code"].transform("count")
    d["alpha191_mode"] = mode
    d["alpha191_factors"] = ",".join(factors)

    if keep_top_frac is not None:
        threshold = np.ceil(d["alpha191_candidates_in_day"] * keep_top_frac).clip(lower=1)
        d = d[d["alpha191_rank_in_day"] <= threshold].copy()
    d["original_v4_rank"] = d["v4_rank"]
    d["original_v4_score"] = d["v4_score"]
    d["v4_rank"] = d["alpha191_rank_in_day"].fillna(999).astype(int)
    d["v4_score"] = d["alpha191_score"].fillna(-1.0)
    return d.drop(columns=["date", "code6"]).copy()


def _variant_specs(factor_ids: list[str]) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = [{"name": "baseline_trigger_time", "factors": [], "keep": None}]
    for fid in factor_ids:
        specs.append({"name": f"{fid}_rank", "factors": [fid], "keep": None})
        specs.append({"name": f"{fid}_top50", "factors": [fid], "keep": 0.50})
    for name, factors in COMPOSITES.items():
        selected = [fid for fid in factors if fid in factor_ids]
        if selected:
            specs.append({"name": f"{name}_rank", "factors": selected, "keep": None})
            specs.append({"name": f"{name}_top50", "factors": selected, "keep": 0.50})
            specs.append({"name": f"{name}_top33", "factors": selected, "keep": 0.33})
    return specs


def run(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    factor_ids = [item.strip() for item in str(args.factors).split(",") if item.strip()]
    source_path = _build_signal_source(
        candidates_path=Path(args.candidates),
        output_dir=output_dir / "sources",
        start_date=str(args.start_date),
        end_date=str(args.end_date),
        preference_filter=str(args.preference_filter),
        share_cap_cache=Path(args.share_cap_cache),
        require_cap_data=bool(args.require_cap_data),
    )
    base = pd.read_parquet(source_path)
    values_path = output_dir / "alpha191_signal_values.parquet"
    if bool(args.reuse_values) and values_path.exists():
        values = pd.read_parquet(values_path)
    else:
        values = _load_alpha_values(base, factor_ids, str(args.start_date), str(args.end_date))
    values.to_parquet(output_dir / "alpha191_signal_values.parquet", index=False)
    values.to_csv(output_dir / "alpha191_signal_values.csv", index=False, encoding="utf-8-sig")
    if args.direction_mode == "candidate_train":
        directions = _candidate_directions(
            base,
            values,
            factor_ids,
            target=str(args.direction_target),
            start_date="2024-07-09",
            end_date="2025-03-31",
        )
    elif args.direction_mode == "candidate_full":
        directions = _candidate_directions(
            base,
            values,
            factor_ids,
            target=str(args.direction_target),
            start_date=str(args.start_date),
            end_date=str(args.end_date),
        )
    else:
        directions = _read_factor_directions(
            ROOT / "reports" / "gen2_alpha191_factor_tests" / "alpha191_recent_2y_best_by_factor_with_sharpe.csv",
            factor_ids,
        )

    policy = str(args.policy)
    rows: list[dict[str, Any]] = []
    segment_rows: list[dict[str, Any]] = []
    variant_dir = output_dir / "variant_sources"
    variant_dir.mkdir(parents=True, exist_ok=True)
    for spec in _variant_specs(factor_ids):
        name = str(spec["name"])
        print(f"backtesting {name}", flush=True)
        if not spec["factors"]:
            variant = base.copy()
        else:
            variant = _score_signal_source(
                base,
                values,
                directions=directions,
                factors=list(spec["factors"]),
                mode=name,
                keep_top_frac=spec["keep"],
            )
        variant_path = variant_dir / f"{name}.parquet"
        variant.to_parquet(variant_path, index=False)
        variant.to_csv(variant_path.with_suffix(".csv"), index=False, encoding="utf-8-sig")
        run_dir = output_dir / "backtests" / name / policy
        summary = _run_dynamic(
            signal_source=variant_path,
            output_dir=run_dir,
            policy=policy,
            start_date=str(args.start_date),
            end_date=str(args.end_date),
            sort_mode="trigger_time" if name == "baseline_trigger_time" else "rank",
        )
        curve = pd.read_csv(run_dir / "equity_curve.csv")
        metrics = _metric_from_curve(curve)
        row = {
            "variant": name,
            "policy": policy,
            "factors": ",".join(spec["factors"]),
            "keep_top_frac": spec["keep"],
            "input_signal_count": int(len(variant)),
            **summary,
            **metrics,
        }
        rows.append(row)
        for seg in _segment_metrics(curve):
            segment_rows.append({"variant": name, **seg})

    result = pd.DataFrame(rows)
    result = result.sort_values(["daily_sharpe", "total_return", "max_drawdown"], ascending=[False, False, False])
    result.to_csv(output_dir / "alpha191_overlay_summary.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(segment_rows).to_csv(output_dir / "alpha191_overlay_segments.csv", index=False, encoding="utf-8-sig")
    payload = {
        "schema_version": 1,
        "policy": policy,
        "factor_ids": factor_ids,
        "directions": directions,
        "rows": result.where(pd.notna(result), None).to_dict("records"),
        "outputs": {
            "summary": "alpha191_overlay_summary.csv",
            "segments": "alpha191_overlay_segments.csv",
            "signal_values": "alpha191_signal_values.csv",
        },
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Test Alpha191 overlays on the G2 risk_cool signal layer.")
    parser.add_argument("--candidates", default=str(DEFAULT_CANDIDATES))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--start-date", default="2024-07-09")
    parser.add_argument("--end-date", default="2026-05-21")
    parser.add_argument("--policy", default="two_stop_cd3_skip")
    parser.add_argument("--preference-filter", choices=["off", "user_v2"], default="user_v2")
    parser.add_argument("--share-cap-cache", default=str(DEFAULT_SHARE_CAP_CACHE))
    parser.add_argument("--require-cap-data", action="store_true")
    parser.add_argument("--factors", default=",".join(DEFAULT_FACTORS))
    parser.add_argument("--reuse-values", action="store_true")
    parser.add_argument("--direction-mode", choices=["factor_report", "candidate_train", "candidate_full"], default="factor_report")
    parser.add_argument("--direction-target", default="outcome_fwd_ret_10d")
    print(json.dumps(run(parser.parse_args()), ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()

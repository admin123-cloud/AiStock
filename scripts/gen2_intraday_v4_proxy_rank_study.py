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
from scripts.gen2_compare_prev_low_exit_fills import FillProfile, _run_profile  # noqa: E402


DEFAULT_CANDIDATES = ROOT / "reports" / "gen2_realtime_candidate_recall_study" / "realtime_candidates.parquet"
DEFAULT_OUTPUT_DIR = ROOT / "reports" / "gen2_intraday_v4_proxy_rank_study"


def _clip01(value: pd.Series) -> pd.Series:
    return value.clip(lower=0.0, upper=1.0)


def _load_candidates(path: Path, pool: str) -> pd.DataFrame:
    d = pd.read_parquet(path)
    d = d[d["realtime_pool"].eq(pool)].copy()
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    d["confirm_datetime"] = pd.to_datetime(d["confirm_datetime"], errors="coerce")
    for col in [
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
        "entry_price",
    ]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    return d.dropna(subset=["entry_date", "code", "confirm_datetime", "entry_price"]).reset_index(drop=True)


def _add_proxy_scores(candidates: pd.DataFrame, score_version: str = "v1") -> pd.DataFrame:
    d = candidates.copy()
    rt_return = d["rt_return_from_d1_close"].fillna(0.0)
    if score_version == "v1":
        rank_quality = _clip01((220.0 - d["v4_rank"].fillna(999.0)) / 220.0)
        score_quality = _clip01((d["v4_score"].fillna(0.0) - 0.55) / 0.35)
        d1_quality = (0.55 * rank_quality + 0.45 * score_quality).fillna(0.0)
        rt_return_score = _clip01((rt_return + 0.02) / 0.16)
        rt_return_score = rt_return_score.where(rt_return <= 0.20, 0.35)
        ma_score = _clip01((d["rt_confirm_vs_ma5"].fillna(0.0) + 0.015) / 0.08)
        ma10_score = _clip01((d["rt_confirm_vs_ma10"].fillna(0.0) + 0.015) / 0.10)
        amount_score = _clip01((d["rt_30m_amount_ratio"].fillna(1.0) - 1.2) / 4.0)
        rebound_score = _clip01((d["rt_fractal_rebound"].fillna(0.0) - 0.02) / 0.16)
        rt_strength = 0.30 * rt_return_score + 0.20 * ma_score + 0.15 * ma10_score + 0.25 * amount_score + 0.10 * rebound_score
        not_overheated = (
            1.0
            - 0.28 * _clip01((d["mom20"].fillna(0.0) - 0.35) / 0.45)
            - 0.22 * _clip01((d["mom5"].fillna(0.0) - 0.16) / 0.22)
            - 0.20 * _clip01((d["vol_ratio"].fillna(1.0) - 2.2) / 2.0)
            - 0.18 * _clip01((rt_return - 0.16) / 0.16)
            - 0.12 * _clip01((d["rt_fractal_rebound"].fillna(0.0) - 0.20) / 0.20)
        ).clip(lower=0.0, upper=1.0)
        early_bonus = np.where(d["rt_confirm_hour"].fillna(15.0) <= 10.5, 0.04, 0.0)
        score = 0.42 * d1_quality + 0.43 * rt_strength + 0.15 * not_overheated + early_bonus
    elif score_version == "v2":
        rank_sweet = (1.0 - ((d["v4_rank"].fillna(999.0) - 105.0).abs() / 140.0)).clip(0, 1)
        score_ok = _clip01((d["v4_score"].fillna(0.0) - 0.62) / 0.22)
        d1_quality = 0.58 * rank_sweet + 0.42 * score_ok
        cool = (
            1.0
            - 0.30 * _clip01((d["mom20"].fillna(0.0) - 0.22) / 0.35)
            - 0.26 * _clip01((d["mom5"].fillna(0.0) - 0.12) / 0.20)
            - 0.22 * _clip01((d["vol_ratio"].fillna(1.0) - 1.65) / 1.5)
            - 0.22 * _clip01((rt_return - 0.12) / 0.14)
        ).clip(0, 1)
        rt_return_sweet = (1.0 - ((rt_return - 0.055).abs() / 0.10)).clip(0, 1)
        amount_sweet = (1.0 - ((d["rt_30m_amount_ratio"].fillna(1.0) - 3.5).abs() / 4.5)).clip(0, 1)
        ma_sweet = (1.0 - ((d["rt_confirm_vs_ma5"].fillna(0.0) - 0.035).abs() / 0.09)).clip(0, 1)
        rebound_sweet = (1.0 - ((d["rt_fractal_rebound"].fillna(0.0) - 0.09).abs() / 0.14)).clip(0, 1)
        rt_strength = 0.32 * rt_return_sweet + 0.28 * amount_sweet + 0.20 * ma_sweet + 0.20 * rebound_sweet
        early_bonus = np.where(d["rt_confirm_hour"].fillna(15.0) <= 10.5, 0.03, 0.0)
        score = 0.34 * d1_quality + 0.40 * rt_strength + 0.26 * cool + early_bonus
        not_overheated = cool
    else:
        raise ValueError(f"Unsupported score_version: {score_version}")
    d["intraday_v4_proxy_score"] = score.clip(0, 1)
    d["intraday_v4_proxy_rank"] = (
        d.sort_values(["entry_date", "intraday_v4_proxy_score", "confirm_datetime", "code"], ascending=[True, False, True, True])
        .groupby("entry_date")
        .cumcount()
        + 1
    )
    d["proxy_d1_quality"] = d1_quality
    d["proxy_rt_strength"] = rt_strength
    d["proxy_not_overheated"] = not_overheated
    d["proxy_score_version"] = score_version
    return d


def _variant_mask(d: pd.DataFrame, variant: str) -> pd.Series:
    if variant == "top1_score_ge_55":
        return d["intraday_v4_proxy_rank"].le(1) & d["intraday_v4_proxy_score"].ge(0.55)
    if variant == "top2_score_ge_55":
        return d["intraday_v4_proxy_rank"].le(2) & d["intraday_v4_proxy_score"].ge(0.55)
    if variant.startswith("top"):
        n = int(variant.replace("top", ""))
        return d["intraday_v4_proxy_rank"].le(n)
    if variant.startswith("score_ge_"):
        threshold = float(variant.replace("score_ge_", "")) / 100.0
        return d["intraday_v4_proxy_score"].ge(threshold)
    raise ValueError(f"Unknown variant: {variant}")


def _prepare_signal_source(d: pd.DataFrame) -> pd.DataFrame:
    out = d.copy()
    out["pattern"] = "pullback_restart_rank100"
    out["g2_open_state"] = "NORMAL"
    out["trigger_type"] = "bottom_fractal_break_high_vol"
    out["original_v4_rank"] = out["v4_rank"]
    out["original_v4_score"] = out["v4_score"]
    out["v4_rank"] = out["intraday_v4_proxy_rank"].astype(int)
    out["v4_score"] = out["intraday_v4_proxy_score"].astype(float)
    return out


def _summarize_proxy(d: pd.DataFrame, variants: list[str]) -> pd.DataFrame:
    target_total = int(d["candidate_key"][d["is_same_day_target"]].nunique())
    rows: list[dict[str, Any]] = []
    for variant in variants:
        selected = d[_variant_mask(d, variant).fillna(False)].copy()
        keys = selected["candidate_key"].dropna().astype(str)
        hits = selected.loc[selected["is_same_day_target"], "candidate_key"].dropna().astype(str).nunique()
        rows.append(
            {
                "variant": variant,
                "signals": int(keys.nunique()),
                "days": int(selected["entry_date"].nunique()) if not selected.empty else 0,
                "target_hits": int(hits),
                "target_total_in_pool": target_total,
                "pool_hit_recall": hits / target_total if target_total else np.nan,
                "precision_vs_pool_targets": hits / keys.nunique() if keys.nunique() else np.nan,
                "avg_proxy_score": float(selected["intraday_v4_proxy_score"].mean()) if not selected.empty else np.nan,
            }
        )
    return pd.DataFrame(rows)


def _write_report(output_dir: Path, recall: pd.DataFrame, backtests: pd.DataFrame) -> None:
    lines = [
        "# G2 Intraday V4 Proxy Rank Study",
        "",
        "Proxy uses D-1 V4 quality plus target-day realtime 30m confirmation strength and overheating penalties.",
        "",
        "## Target Recall Inside Candidate Pool",
        "",
        "| variant | signals | days | target_hits | pool_recall | precision | avg_score |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for _, row in recall.iterrows():
        lines.append(
            f"| {row['variant']} | {int(row['signals'])} | {int(row['days'])} | {int(row['target_hits'])} | "
            f"{_pct(row['pool_hit_recall'])} | {_pct(row['precision_vs_pool_targets'])} | {row['avg_proxy_score']:.4f} |"
        )
    lines.extend(["", "## Portfolio Backtest", ""])
    lines.append("| variant | signals | trades | total | excess_vs_csi1000 | max_dd | win | avg_trade |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for _, row in backtests.iterrows():
        lines.append(
            f"| {row['variant']} | {int(row.get('signal_count') or 0)} | {int(row.get('trade_count') or 0)} | "
            f"{_pct(row.get('total_return'))} | {_pct(row.get('excess_return'))} | {_pct(row.get('max_drawdown'))} | "
            f"{_pct(row.get('win_rate'))} | {_pct(row.get('avg_trade_return'))} |"
        )
    (output_dir / "proxy_rank_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    variants = [x.strip() for x in str(args.variants).split(",") if x.strip()]
    candidates = _add_proxy_scores(_load_candidates(Path(args.candidates), str(args.pool)), str(args.score_version))
    candidates.to_csv(output_dir / "proxy_candidates.csv", index=False, encoding="utf-8-sig")
    candidates.to_parquet(output_dir / "proxy_candidates.parquet", index=False)
    recall = _summarize_proxy(candidates, variants)

    profile = FillProfile("gap_confirm_30m_close__intraday_30m_close", gap_open_mode="confirm_30m", intraday_mode="bar_close")
    backtest_rows: list[dict[str, Any]] = []
    for variant in variants:
        selected = candidates[_variant_mask(candidates, variant).fillna(False)].copy()
        signal_source = output_dir / "sources" / f"{variant}.parquet"
        signal_source.parent.mkdir(parents=True, exist_ok=True)
        source_df = _prepare_signal_source(selected)
        source_df.to_parquet(signal_source, index=False)
        source_df.to_csv(signal_source.with_suffix(".csv"), index=False, encoding="utf-8-sig")
        summary = _run_profile(
            signal_source=signal_source,
            output_dir=output_dir / "backtests" / variant,
            profile=profile,
            start_date=str(args.start_date),
            end_date=str(args.end_date),
            sort_mode="rank",
        )
        summary["variant"] = variant
        backtest_rows.append(summary)

    backtests = pd.DataFrame(backtest_rows)
    recall.to_csv(output_dir / "proxy_recall_summary.csv", index=False, encoding="utf-8-sig")
    backtests.to_csv(output_dir / "proxy_backtest_summary.csv", index=False, encoding="utf-8-sig")
    _write_report(output_dir, recall, backtests)
    payload = {
        "schema_version": 1,
        "candidates": str(Path(args.candidates)),
        "pool": str(args.pool),
        "score_version": str(args.score_version),
        "variants": variants,
        "start_date": str(args.start_date),
        "end_date": str(args.end_date),
        "recall": recall.where(pd.notna(recall), None).to_dict("records"),
        "backtests": backtests.where(pd.notna(backtests), None).to_dict("records"),
        "outputs": {
            "report": "proxy_rank_report.md",
            "proxy_candidates": "proxy_candidates.parquet",
            "recall": "proxy_recall_summary.csv",
            "backtests": "proxy_backtest_summary.csv",
        },
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Build and backtest a first intraday V4 proxy rank for G2 realtime candidates.")
    parser.add_argument("--candidates", default=str(DEFAULT_CANDIDATES))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--pool", default="d1_rank200")
    parser.add_argument("--score-version", default="v1", choices=["v1", "v2"])
    parser.add_argument("--variants", default="top1,top2,top3,top5,top1_score_ge_55,top2_score_ge_55,score_ge_55,score_ge_60")
    parser.add_argument("--start-date", default="2024-07-09")
    parser.add_argument("--end-date", default="2026-05-21")
    print(json.dumps(run(parser.parse_args()), ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()

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

from scripts.gen3_validate_mainline_icepoint_pullback_v1 import (  # noqa: E402
    _add_pullback_features,
    _annual_summary,
    _bool_series,
    _market_regime,
    _parse_horizons,
    _safe_float,
    _select_mainline_windows,
    _summarize,
)
from scripts.research_main_wave_sector_score import (  # noqa: E402
    _add_sector_feature_frame,
    _add_stock_features,
    _build_sector_daily,
    _load_daily,
    _load_members,
)
from scripts.validate_true_sector_index_intraday_diffusion_v1 import (  # noqa: E402
    _confirm_diffusion,
    _load_intraday_day,
    _morning_stock_returns,
    _sector_diffusion_stats,
)
from utils.paths import report_path  # noqa: E402


OUT_DIR = report_path("gen3_mainline_icepoint_intraday_diffusion_validation_v1")


def _load_stock_pool() -> pd.DataFrame:
    from scripts.gen3_validate_mainline_icepoint_pullback_v1 import _load_stock_pool as load_stock_pool

    return load_stock_pool()


def _pct(value: Any) -> str:
    x = _safe_float(value)
    if x is None:
        return ""
    return f"{x:.2%}"


def _prepare_base_candidates(args: argparse.Namespace, horizons: list[int]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    max_h = max(horizons)
    load_start = (pd.Timestamp(args.start_date) - pd.Timedelta(days=int(args.lookback_days))).strftime("%Y-%m-%d")
    load_end = (pd.Timestamp(args.end_date) + pd.Timedelta(days=max_h + 10)).strftime("%Y-%m-%d")

    members = _load_members([int(args.level)], int(args.min_members))
    raw_daily = _load_daily(load_start, load_end)
    stock_pool = _load_stock_pool()
    daily = raw_daily.merge(stock_pool[["code6", "stock_name"]], on="code6", how="inner")
    daily = daily[(daily["trade_date"] >= pd.Timestamp(load_start)) & (daily["trade_date"] <= pd.Timestamp(load_end))].copy()

    stock_features = _add_stock_features(daily)
    sector_daily = _build_sector_daily(stock_features, members)
    sector_features = _add_sector_feature_frame(sector_daily)
    mainline_windows = _select_mainline_windows(
        sector_features,
        int(args.level),
        int(args.top_n),
        float(args.min_mainline_score),
    )

    market_env = _market_regime(daily, args.start_date, args.end_date)
    stock_signal = _add_pullback_features(daily, horizons)
    stock_signal = stock_signal.merge(market_env, on="trade_date", how="left")
    stock_signal = stock_signal[
        (stock_signal["trade_date"] >= pd.Timestamp(args.start_date))
        & (stock_signal["trade_date"] <= pd.Timestamp(args.end_date))
        & stock_signal["regime"].isin(["trend_up", "range"])
    ].copy()

    win_keep = [
        "trade_date",
        "sector_code",
        "sector_name",
        "mainline_window_score",
        "mainline_rank",
        "sector_ret",
        "rise_ratio",
        "strong3_ratio",
        "amount_ratio5_20",
        "ret5",
        "ret10",
        "ret20",
        "ret60",
        "relative_ret60",
        "rank_ret60_pct",
    ]
    win_keys = mainline_windows[[c for c in win_keep if c in mainline_windows.columns]].copy()
    stock_mainline = stock_signal.merge(
        members[["stock_code6", "sector_code"]].drop_duplicates(),
        left_on="code6",
        right_on="stock_code6",
        how="left",
    ).merge(win_keys, on=["trade_date", "sector_code"], how="left")
    stock_mainline["in_mainline"] = stock_mainline["mainline_window_score"].notna()

    base = stock_mainline[
        stock_mainline["strong_consolidation"]
        & stock_mainline["in_mainline"]
        & stock_mainline["stock_pullback_age_4_8"]
        & _bool_series(stock_mainline, "emotion_repair35_55_after_18_32")
    ].copy()

    return base, members, daily


def _thresholds() -> dict[str, dict[str, float]]:
    return {
        "loose": {"min_rise_ratio": 0.52, "min_strong2_ratio": 0.04, "min_avg_ret": 0.004},
        "base": {"min_rise_ratio": 0.55, "min_strong2_ratio": 0.06, "min_avg_ret": 0.006},
        "strict": {"min_rise_ratio": 0.60, "min_strong2_ratio": 0.08, "min_avg_ret": 0.008},
    }


def _build_intraday_stats(base: pd.DataFrame, members: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    if base.empty:
        return pd.DataFrame()

    rows: list[pd.DataFrame] = []
    candidate_windows = (
        base[["trade_date", "sector_code", "sector_name"]]
        .dropna(subset=["trade_date", "sector_code"])
        .drop_duplicates()
        .copy()
    )
    candidate_windows["anchor_date"] = pd.to_datetime(candidate_windows["trade_date"]).dt.strftime("%Y-%m-%d")
    cutoffs = [x.strip() for x in str(args.cutoff_times).split(",") if x.strip()]

    for anchor_date, window_rows in candidate_windows.groupby("anchor_date"):
        intraday = _load_intraday_day(args.table, anchor_date)
        if intraday.empty:
            continue
        for cutoff in cutoffs:
            morning = _morning_stock_returns(intraday, cutoff)
            stats = _sector_diffusion_stats(morning, members, window_rows, int(args.min_visible_members))
            if stats.empty:
                continue
            stats = stats.copy()
            stats["anchor_date"] = anchor_date
            stats["cutoff_time"] = cutoff
            scored = _confirm_diffusion(stats, 0.0, 0.0, -1.0)
            score_map = scored.set_index("sector_code")["intraday_diffusion_score"].to_dict() if not scored.empty else {}
            stats["intraday_diffusion_score"] = stats["sector_code"].astype(str).map(score_map)
            for level, cfg in _thresholds().items():
                confirmed = _confirm_diffusion(
                    stats,
                    cfg["min_rise_ratio"],
                    cfg["min_strong2_ratio"],
                    cfg["min_avg_ret"],
                )
                stats[f"diffusion_{level}"] = stats["sector_code"].astype(str).isin(
                    set(confirmed.get("sector_code", pd.Series(dtype=str)).astype(str))
                )
            rows.append(stats)

    if not rows:
        return pd.DataFrame()
    out = pd.concat(rows, ignore_index=True)
    out["trade_date"] = pd.to_datetime(out["anchor_date"], errors="coerce")
    return out


def _merge_intraday(base: pd.DataFrame, intraday_stats: pd.DataFrame) -> pd.DataFrame:
    if base.empty or intraday_stats.empty:
        return base.copy()
    keep = [
        "trade_date",
        "sector_code",
        "cutoff_time",
        "visible_members",
        "morning_ret_avg",
        "morning_ret_median",
        "morning_rise_ratio",
        "morning_strong1_ratio",
        "morning_strong2_ratio",
        "morning_strong3_ratio",
        "morning_weak_ratio",
        "intraday_diffusion_score",
        "diffusion_loose",
        "diffusion_base",
        "diffusion_strict",
    ]
    s = intraday_stats[[c for c in keep if c in intraday_stats.columns]].copy()
    merged = base.merge(s, on=["trade_date", "sector_code"], how="left")
    return merged


def _samples_by_group(merged: pd.DataFrame, horizons: list[int]) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    samples: dict[str, pd.DataFrame] = {"daily_base": merged.drop_duplicates(["trade_date", "code6", "sector_code"]).copy()}
    if "cutoff_time" not in merged.columns:
        summary_rows: list[dict[str, Any]] = []
        summary_rows.extend(_summarize("daily_base", samples["daily_base"], horizons))
        return pd.DataFrame(summary_rows), samples
    for cutoff in sorted(merged["cutoff_time"].dropna().astype(str).unique()):
        part = merged[merged["cutoff_time"].astype(str).eq(cutoff)].copy()
        suffix = cutoff.replace(":", "")
        for level in ["loose", "base", "strict"]:
            col = f"diffusion_{level}"
            if col not in part.columns:
                samples[f"diff_{level}_{suffix}"] = part.iloc[0:0].copy()
            else:
                samples[f"diff_{level}_{suffix}"] = part[part[col].fillna(False).astype(bool)].copy()

    summary_rows: list[dict[str, Any]] = []
    for label, sample in samples.items():
        sample_unique = sample.drop_duplicates(["trade_date", "code6", "sector_code"]).copy()
        samples[label] = sample_unique
        summary_rows.extend(_summarize(label, sample_unique, horizons))
    return pd.DataFrame(summary_rows), samples


def _write_outputs(result: dict[str, Any], merged: pd.DataFrame, intraday_stats: pd.DataFrame, summary: pd.DataFrame, samples: dict[str, pd.DataFrame], horizons: list[int]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    intraday_stats.to_csv(OUT_DIR / "intraday_sector_stats.csv", index=False, encoding="utf-8-sig")
    merged.to_csv(OUT_DIR / "candidate_with_intraday.csv", index=False, encoding="utf-8-sig")

    best_label = "diff_base_113000" if "diff_base_113000" in samples else next(iter(samples))
    annual = _annual_summary(samples.get(best_label, pd.DataFrame()), horizons)
    annual.to_csv(OUT_DIR / f"annual_{best_label}.csv", index=False, encoding="utf-8-sig")

    examples = samples.get(best_label, pd.DataFrame()).copy()
    keep_cols = [
        "trade_date",
        "code_raw",
        "stock_name",
        "sector_name",
        "regime",
        "close_up_rate",
        "emotion_source",
        "mainline_window_score",
        "sector_ret",
        "rise_ratio",
        "strong3_ratio",
        "morning_ret_avg",
        "morning_rise_ratio",
        "morning_strong2_ratio",
        "intraday_diffusion_score",
        "pullback_from_high20",
        "distance_ma20",
        "days_since_high20",
        "ret1",
    ] + [f"fwd_ret_{h}" for h in horizons]
    if not examples.empty:
        examples = examples.sort_values(["trade_date", "intraday_diffusion_score", "mainline_window_score"], ascending=[False, False, False])
        examples = examples[[c for c in keep_cols if c in examples.columns]].head(200).copy()
        for col in examples.columns:
            if pd.api.types.is_datetime64_any_dtype(examples[col]):
                examples[col] = examples[col].dt.strftime("%Y-%m-%d")
    examples.to_csv(OUT_DIR / f"examples_{best_label}.csv", index=False, encoding="utf-8-sig")

    pivot = summary.pivot_table(index="horizon", columns="group", values="avg", aggfunc="first").reset_index()
    pivot.to_csv(OUT_DIR / "comparison_avg.csv", index=False, encoding="utf-8-sig")

    (OUT_DIR / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# G3 主线冰点修复 + 30m 主线扩散验证 v1",
        "",
        "## 结论口径",
        "",
        "- 日线底座：主线板块 + 强势盘整 + 回踩 4-8 个交易日 + 全体上涨率 18%-32% 冰点后修复到 35%-55%。",
        "- 盘中确认：同一主线板块在 30m 数据下，截至 10:30 或 11:30 出现板块成员扩散。",
        "- 本版只验证主线板块盘中承接，不混入个股 30m 承接买点。",
        "",
        "## 样本规模",
        "",
        f"- 日线底座样本：{result.get('base_rows')}",
        f"- 有盘中统计的候选样本：{result.get('merged_rows')}",
        f"- 盘中板块统计行：{result.get('intraday_stat_rows')}",
        "",
        "## 收益汇总",
        "",
        summary.to_markdown(index=False),
        "",
        "## 分年",
        "",
        annual.to_markdown(index=False) if not annual.empty else "无分年样本。",
        "",
        "## 文件",
        "",
        f"- summary.csv: {OUT_DIR / 'summary.csv'}",
        f"- candidate_with_intraday.csv: {OUT_DIR / 'candidate_with_intraday.csv'}",
        f"- intraday_sector_stats.csv: {OUT_DIR / 'intraday_sector_stats.csv'}",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines), encoding="utf-8")


def run(args: argparse.Namespace) -> dict[str, Any]:
    horizons = _parse_horizons(args.horizons)
    base, members, daily = _prepare_base_candidates(args, horizons)
    intraday_stats = _build_intraday_stats(base, members, args)
    merged = _merge_intraday(base, intraday_stats)
    summary, samples = _samples_by_group(merged, horizons)

    result = {
        "status": "completed",
        "start_date": args.start_date,
        "end_date": args.end_date,
        "level": int(args.level),
        "top_n": int(args.top_n),
        "min_mainline_score": float(args.min_mainline_score),
        "horizons": horizons,
        "daily_rows": int(len(daily)),
        "base_rows": int(len(base)),
        "intraday_stat_rows": int(len(intraday_stats)),
        "merged_rows": int(len(merged)),
        "thresholds": _thresholds(),
        "out_dir": str(OUT_DIR),
    }
    for label, sample in samples.items():
        result[f"{label}_rows"] = int(len(sample))
    _write_outputs(result, merged, intraday_stats, summary, samples, horizons)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate G3 mainline icepoint repair with 30m sector diffusion.")
    parser.add_argument("--start-date", default="2020-01-01")
    parser.add_argument("--end-date", default="2026-05-29")
    parser.add_argument("--level", type=int, default=2)
    parser.add_argument("--top-n", type=int, default=8)
    parser.add_argument("--min-members", type=int, default=8)
    parser.add_argument("--min-visible-members", type=int, default=15)
    parser.add_argument("--min-mainline-score", type=float, default=55.0)
    parser.add_argument("--horizons", default="4,5,8,10")
    parser.add_argument("--lookback-days", type=int, default=160)
    parser.add_argument("--table", default="kline_minute_30")
    parser.add_argument("--cutoff-times", default="10:30:00,11:30:00")
    args = parser.parse_args()
    result = run(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

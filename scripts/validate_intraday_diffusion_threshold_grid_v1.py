from __future__ import annotations

import argparse
import gc
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.research_main_wave_sector_score import _add_stock_features, _load_daily, _load_members
from scripts.validate_mainline_window_stock_acceptance import (
    _add_stock_acceptance_features,
    _comparison,
    _forward_return_columns,
    _load_stock_pool,
    _pct,
)
from scripts.validate_quarterly_mainline_stock_acceptance_cached_v1 import _parse_years
from scripts.validate_quarterly_mainline_stock_acceptance_v1 import _parse_horizons, _window_samples
from scripts.validate_true_sector_index_intraday_diffusion_v1 import (
    _confirm_diffusion,
    _load_intraday_day,
    _load_true_windows,
    _morning_stock_returns,
    _sector_diffusion_stats,
    _year_load_start,
)


OUT_DIR = ROOT / "reports" / "intraday_diffusion_threshold_grid_v1"


THRESHOLDS = [
    {"name": "loose", "min_rise_ratio": 0.52, "min_strong2_ratio": 0.04, "min_avg_ret": 0.004},
    {"name": "base", "min_rise_ratio": 0.55, "min_strong2_ratio": 0.06, "min_avg_ret": 0.006},
    {"name": "strict", "min_rise_ratio": 0.60, "min_strong2_ratio": 0.08, "min_avg_ret": 0.008},
]


def _aggregate_summary(summary: pd.DataFrame) -> list[dict[str, Any]]:
    if summary.empty:
        return []
    agg = summary.groupby(["group", "horizon"], as_index=False).agg(avg=("avg", "mean"))
    wide = agg.pivot_table(index="horizon", columns="group", values="avg", aggfunc="first").reset_index()
    rows: list[dict[str, Any]] = []
    for row in wide.sort_values("horizon").itertuples(index=False):
        main_accept = getattr(row, "mainline_sector_acceptance", None)
        market_accept = getattr(row, "market_acceptance", None)
        rows.append(
            {
                "horizon": int(row.horizon),
                "mainline_sector_acceptance": main_accept,
                "market_acceptance": market_accept,
                "diff": None if main_accept is None or market_accept is None else main_accept - market_accept,
                "mainline_sector_all": getattr(row, "mainline_sector_all", None),
                "market_all": getattr(row, "market_all", None),
            }
        )
    return rows


def _run_grid(args: argparse.Namespace) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    years = _parse_years(args.years)
    horizons = _parse_horizons(args.horizons)
    windows_df = _load_true_windows(years)
    members = _load_members([args.level], 5)
    stock_pool = _load_stock_pool()

    stats_parts: list[pd.DataFrame] = []
    summary_parts: list[pd.DataFrame] = []
    comparison_parts: list[pd.DataFrame] = []

    for year in years:
        print(f"YEAR {year} start")
        year_windows = windows_df[windows_df["year"].eq(year)].copy()
        if year_windows.empty:
            continue
        first_anchor = year_windows["anchor_date"].min()
        load_start = _year_load_start(first_anchor, args.lookback_days)
        daily = _load_daily(load_start, f"{year}-12-31")
        stock_daily = _forward_return_columns(_add_stock_acceptance_features(_add_stock_features(daily)), horizons)

        by_anchor_stats: dict[str, pd.DataFrame] = {}
        for anchor, wr in year_windows.groupby("anchor_date"):
            intraday = _load_intraday_day(args.table, anchor)
            morning = _morning_stock_returns(intraday, args.cutoff_time)
            by_anchor_stats[anchor] = _sector_diffusion_stats(morning, members, wr, args.min_visible_members)

        for threshold in THRESHOLDS:
            year_stats: list[pd.DataFrame] = []
            year_summary: list[pd.DataFrame] = []
            for anchor, stats in by_anchor_stats.items():
                confirmed = _confirm_diffusion(
                    stats,
                    threshold["min_rise_ratio"],
                    threshold["min_strong2_ratio"],
                    threshold["min_avg_ret"],
                )
                if confirmed.empty:
                    continue
                confirmed = confirmed.copy()
                confirmed["anchor_date"] = anchor
                confirmed["year"] = int(year)
                confirmed["threshold"] = threshold["name"]
                year_stats.append(confirmed)
                summary_rows, _detail = _window_samples(
                    stock_daily,
                    members,
                    stock_pool,
                    {"anchor_date": anchor, "top_sectors": confirmed["sector_code"].astype(str).tolist()},
                    horizons,
                )
                if summary_rows:
                    s = pd.DataFrame(summary_rows)
                    s["threshold"] = threshold["name"]
                    year_summary.append(s)
            if year_stats:
                stats_parts.append(pd.concat(year_stats, ignore_index=True))
            if year_summary:
                s_all = pd.concat(year_summary, ignore_index=True)
                summary_parts.append(s_all)
                cmp = _comparison(s_all)
                cmp["threshold"] = threshold["name"]
                comparison_parts.append(cmp)
        del daily, stock_daily
        gc.collect()

    stats = pd.concat(stats_parts, ignore_index=True) if stats_parts else pd.DataFrame()
    summary = pd.concat(summary_parts, ignore_index=True) if summary_parts else pd.DataFrame()
    comparison = pd.concat(comparison_parts, ignore_index=True) if comparison_parts else pd.DataFrame()
    return stats, summary, comparison


def _write_outputs(args: argparse.Namespace, stats: pd.DataFrame, summary: pd.DataFrame, comparison: pd.DataFrame) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stats_path = OUT_DIR / "threshold_stats.csv"
    summary_path = OUT_DIR / "threshold_summary.csv"
    comparison_path = OUT_DIR / "threshold_comparison.csv"
    report_path = OUT_DIR / "REPORT.md"
    json_path = OUT_DIR / "run_summary.json"
    stats.to_csv(stats_path, index=False, encoding="utf-8-sig")
    summary.to_csv(summary_path, index=False, encoding="utf-8-sig")
    comparison.to_csv(comparison_path, index=False, encoding="utf-8-sig")

    result = {
        "years": _parse_years(args.years),
        "level": args.level,
        "horizons": _parse_horizons(args.horizons),
        "table": args.table,
        "cutoff_time": args.cutoff_time,
        "thresholds": THRESHOLDS,
        "stats_csv": str(stats_path),
        "summary_csv": str(summary_path),
        "comparison_csv": str(comparison_path),
        "report": str(report_path),
    }
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# 盘中扩散阈值稳健性验证 V1",
        "",
        f"- 分钟周期：`{args.table}`",
        f"- 截止时间：`{args.cutoff_time}`",
        "- 三组阈值：loose/base/strict，分别提高上涨比例、2%以上强势比例和上午平均涨幅要求。",
        "",
        "## 汇总结果",
        "",
        "| 阈值 | 持有天数 | 主线承接股 | 全市场承接股 | 差值 | 主线全体股 | 全市场全体股 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for threshold in [x["name"] for x in THRESHOLDS]:
        rows = _aggregate_summary(summary[summary["threshold"].eq(threshold)].copy())
        for row in rows:
            lines.append(
                f"| {threshold} | {row['horizon']} | {_pct(row['mainline_sector_acceptance'])} | "
                f"{_pct(row['market_acceptance'])} | {_pct(row['diff'])} | "
                f"{_pct(row['mainline_sector_all'])} | {_pct(row['market_all'])} |"
            )
    lines.extend(
        [
            "",
            "## 结论口径",
            "",
            "- 若三组阈值均为正，说明盘中扩散具有稳健性。",
            "- 若只有 base 为正，说明参数敏感，需要谨慎。",
            "- 若 strict 仍为正但样本明显减少，可作为更保守的实盘观察条件。",
        ]
    )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(str(report_path))
    print(str(comparison_path))
    for threshold in [x["name"] for x in THRESHOLDS]:
        print("THRESHOLD", threshold)
        for row in _aggregate_summary(summary[summary["threshold"].eq(threshold)].copy()):
            print(
                f"h{row['horizon']} main_accept={_pct(row['mainline_sector_acceptance'])} "
                f"market_accept={_pct(row['market_acceptance'])} diff={_pct(row['diff'])}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="Intraday diffusion threshold grid.")
    parser.add_argument("--years", default="2020-2025")
    parser.add_argument("--level", type=int, default=2)
    parser.add_argument("--horizons", default="20,60,120")
    parser.add_argument("--lookback-days", type=int, default=160)
    parser.add_argument("--table", default="kline_minute_60")
    parser.add_argument("--cutoff-time", default="11:30:00")
    parser.add_argument("--min-visible-members", type=int, default=15)
    args = parser.parse_args()
    stats, summary, comparison = _run_grid(args)
    _write_outputs(args, stats, summary, comparison)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

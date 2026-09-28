from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any, Callable

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_test_range_30m_volume_acceptance_v1 import (
    PROFILES,
    md_table,
    simulate,
    standardize,
    summarize,
    window_metrics,
)


SOURCE_DIR = ROOT / "reports" / "gen3_range_30m_volume_acceptance_v1"
OUT_DIR = ROOT / "reports" / "gen3_range_30m_true_acceptance_structure_v1"


BASE_VARIANTS = [
    {
        "variant": "box_stress_accept_h5",
        "variant_cn": "横盘箱体底部放量承接，持有5日",
        "signal_file": "box_stress_accept_h5_signals.csv",
    },
    {
        "variant": "weak_low_accept_h5",
        "variant_cn": "弱反弹低位不追高放量承接，持有5日",
        "signal_file": "weak_low_accept_h5_signals.csv",
    },
    {
        "variant": "weak_low_accept_h3",
        "variant_cn": "弱反弹低位不追高放量承接，持有3日",
        "signal_file": "weak_low_accept_h3_signals.csv",
    },
]


def _true_mask(_: pd.DataFrame) -> pd.Series:
    return pd.Series(True, index=_.index)


def _strong_bar(d: pd.DataFrame) -> pd.Series:
    return d["amount_ratio3"].ge(1.50) & d["bar_close_pos"].ge(0.75)


def _deep_floor_strong_bar(d: pd.DataFrame) -> pd.Series:
    return _strong_bar(d) & d["range_pos60"].le(0.08)


def _daily_reclaim_strong_bar(d: pd.DataFrame) -> pd.Series:
    return _strong_bar(d) & d["close_position"].ge(0.70)


def _no_gapup_strong_bar(d: pd.DataFrame) -> pd.Series:
    return _strong_bar(d) & d["gap_open"].le(0.02)


FILTERS: list[dict[str, Any]] = [
    {
        "filter": "base",
        "filter_cn": "原始30m承接，不加额外结构",
        "fn": _true_mask,
    },
    {
        "filter": "strong_bar",
        "filter_cn": "30m强承接：量比>=1.5且收盘位置>=75%",
        "fn": _strong_bar,
    },
    {
        "filter": "deep_floor_strong_bar",
        "filter_cn": "贴近箱体底部的30m强承接：箱体位置<=8%，量比>=1.5，收盘位置>=75%",
        "fn": _deep_floor_strong_bar,
    },
    {
        "filter": "daily_reclaim_strong_bar",
        "filter_cn": "日线修复配合30m强承接：日线收盘修复>=70%，30m量比>=1.5且收盘位置>=75%",
        "fn": _daily_reclaim_strong_bar,
    },
    {
        "filter": "no_gapup_strong_bar",
        "filter_cn": "不追高的30m强承接：跳空<=2%，30m量比>=1.5且收盘位置>=75%",
        "fn": _no_gapup_strong_bar,
    },
]


def pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return "--"
    return f"{float(value) * 100:+.2f}%"


def load_signals(spec: dict[str, str]) -> pd.DataFrame:
    path = SOURCE_DIR / spec["signal_file"]
    d = pd.read_csv(path, low_memory=False)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for col in [
        "amount_ratio3",
        "bar_close_pos",
        "range_pos60",
        "close_position",
        "gap_open",
        "index_mom20",
        "candidate_score",
        "rank_key",
        "fwd_ret_confirm_to_close_3d",
        "fwd_ret_confirm_to_close_5d",
    ]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    return d


def summarize_coverage(base: pd.DataFrame, selected: pd.DataFrame, base_variant: str, filter_name: str, filter_cn: str) -> dict[str, Any]:
    raw_ret = pd.to_numeric(selected.get("fwd_ret_confirm_to_close_5d", pd.Series(dtype=float)), errors="coerce")
    return {
        "base_variant": base_variant,
        "filter": filter_name,
        "filter_cn": filter_cn,
        "base_signal_count": int(len(base)),
        "selected_count": int(len(selected)),
        "selected_days": int(selected["entry_date"].nunique()) if len(selected) else 0,
        "select_rate": float(len(selected) / len(base)) if len(base) else 0.0,
        "raw_avg_5d": float(raw_ret.mean()) if len(raw_ret) else 0.0,
        "raw_win_rate": float((raw_ret > 0).mean()) if len(raw_ret) else 0.0,
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    summary_rows: list[dict[str, Any]] = []
    window_rows: list[dict[str, Any]] = []
    coverage_rows: list[dict[str, Any]] = []

    for spec in BASE_VARIANTS:
        base = load_signals(spec)
        for filt in FILTERS:
            selected = base[filt["fn"](base)].copy()
            selected["base_variant"] = spec["variant"]
            selected["base_variant_cn"] = spec["variant_cn"]
            selected["filter"] = filt["filter"]
            selected["filter_cn"] = filt["filter_cn"]
            selected["variant"] = f"{spec['variant']}__{filt['filter']}"
            selected["desc"] = f"{spec['variant_cn']} + {filt['filter_cn']}"
            coverage_rows.append(summarize_coverage(base, selected, spec["variant"], filt["filter"], filt["filter_cn"]))
            selected.to_csv(OUT_DIR / f"{spec['variant']}__{filt['filter']}_signals.csv", index=False, encoding="utf-8-sig")

            for profile in PROFILES:
                candidates = standardize(selected, profile)
                curve, closed = simulate(candidates)
                run_dir = OUT_DIR / f"{spec['variant']}__{filt['filter']}__{profile['profile']}"
                run_dir.mkdir(parents=True, exist_ok=True)
                candidates.to_csv(run_dir / "candidates.csv", index=False, encoding="utf-8-sig")
                curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
                closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
                row = summarize(curve, closed, f"{spec['variant']}__{filt['filter']}", str(profile["profile"]), str(selected["desc"].iloc[0]) if len(selected) else filt["filter_cn"])
                row["base_variant"] = spec["variant"]
                row["base_variant_cn"] = spec["variant_cn"]
                row["filter"] = filt["filter"]
                row["filter_cn"] = filt["filter_cn"]
                summary_rows.append(row)
                window_rows.extend(window_metrics(curve, closed, f"{spec['variant']}__{filt['filter']}", str(profile["profile"])))

    coverage = pd.DataFrame(coverage_rows)
    summary = pd.DataFrame(summary_rows)
    windows = pd.DataFrame(window_rows)
    coverage.to_csv(OUT_DIR / "coverage.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "window_metrics.csv", index=False, encoding="utf-8-sig")

    primary = summary[summary["profile"].eq("cost30")].copy()
    primary = primary.sort_values(["total_return", "max_drawdown"], ascending=[False, False])
    top = primary.head(10)
    pct_cols = {
        "select_rate",
        "raw_avg_5d",
        "raw_win_rate",
        "total_return",
        "max_drawdown",
        "win_rate",
        "avg_trade_return",
        "worst_trade",
        "return",
    }
    result = {
        "signal_backtest_start": "2020-01-01",
        "signal_backtest_end": "2026-05-29",
        "profiles": {
            "cost30": "30bps交易成本口径",
            "cost100": "100bps高摩擦口径",
            "shock2_cost30": "30bps成本并额外扣2%冲击",
        },
        "top_cost30": top[["variant", "base_variant_cn", "filter_cn", "trade_count", "total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade"]].to_dict(orient="records"),
    }
    (OUT_DIR / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# G3 30m真实放量承接结构复验 v1",
        "",
        "## 口径说明",
        "- 本次不是调 `score/rank`，只验证固定结构标签。",
        "- 信号回测窗口：2020-01-01 至 2026-05-29。",
        "- `box_stress_accept_h5`：横盘箱体底部放量承接，持有5日。",
        "- `weak_low_accept_h5`：弱反弹低位不追高放量承接，持有5日。",
        "- `weak_low_accept_h3`：弱反弹低位不追高放量承接，持有3日。",
        "- `strong_bar`：30m量比>=1.5且该30m K线收盘位置>=75%。",
        "- `deep_floor_strong_bar`：在 `strong_bar` 基础上，要求箱体位置<=8%，也就是更贴近箱体底部。",
        "- `daily_reclaim_strong_bar`：在 `strong_bar` 基础上，要求日线收盘修复>=70%。",
        "- `no_gapup_strong_bar`：在 `strong_bar` 基础上，要求入场日跳空<=2%，避免追高。",
        "",
        "## 覆盖率",
        md_table(coverage, pct_cols=pct_cols),
        "",
        "## 30bps Top结果",
        md_table(top[["variant", "base_variant_cn", "filter_cn", "profile", "trade_count", "total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade"]], pct_cols=pct_cols),
        "",
        "## 全部slot复算",
        md_table(summary, pct_cols=pct_cols),
        "",
        "## 分窗口",
        md_table(windows, pct_cols=pct_cols),
        "",
        "## 初步判断",
        "- 如果强承接标签只提高 30bps，但在 100bps 或 2%冲击下仍明显恶化，说明它不是可实盘的稳定买点。",
        "- 如果 2024-2025 验证窗仍为负，则不能用 2026 盲测的短期修复直接升格。",
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

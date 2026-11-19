from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_rebuild_range_box_source_map_v1 import (  # noqa: E402
    PROFILES,
    md_table,
    load_base,
    simulate,
    summarize,
    window_metrics,
)
from scripts.gen3_rebuild_range_box_source_map_v1 import exit_dates  # noqa: E402


OUT_DIR = ROOT / "reports" / "gen3_range_box_structural_entry_v1"

VARIANTS = [
    {
        "variant": "box_first_panic_reclaim",
        "desc": "箱体底部第一次恐慌后修复：箱体低位、近端急跌、收盘修复或下影承接",
    },
    {
        "variant": "box_first_panic_strong_reclaim",
        "desc": "箱体底部强修复恐慌：箱体极低、近端深跌、收盘修复更强",
    },
    {
        "variant": "box_retest_reclaim",
        "desc": "箱体底部二次回踩不破后反抽：低位有过反弹、短线回踩、收盘重新站回高位",
    },
    {
        "variant": "box_retest_low_amount_reclaim",
        "desc": "箱体底部二次回踩缩量修复：二次回踩不破，日线量能不过热",
    },
    {
        "variant": "box_retest_capitulation_reclaim",
        "desc": "箱体底部二次回踩叠加市场出清：二次回踩结构遇到市场恐慌后修复",
    },
]


def pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def mask_for(df: pd.DataFrame, variant: str) -> pd.Series:
    floor10 = df["range_pos60"].le(0.10)
    floor15 = df["range_pos60"].le(0.15)
    deep_near_drop = df["drawdown10"].le(-0.10) | df["drawdown5"].le(-0.06)
    very_deep_near_drop = df["drawdown10"].le(-0.12) | df["drawdown5"].le(-0.08)
    reclaim = df["close_position"].ge(0.60) | df["lower_shadow_ratio"].ge(0.22)
    strong_reclaim = df["close_position"].ge(0.68)
    capitulation = df["big_down_rate"].ge(0.10)
    not_overheat_amount = df["amount_ratio20"].lt(1.0)
    had_bounce = df["runup_from_60d_low"].between(0.04, 0.35, inclusive="both")
    short_pullback = df["drawdown5"].le(-0.025) & df["drawdown20"].ge(-0.22)
    trend_not_chasing = df["mom10"].between(-0.08, 0.12, inclusive="both")
    retest_base = floor15 & had_bounce & short_pullback & trend_not_chasing & df["close_position"].ge(0.58)

    if variant == "box_first_panic_reclaim":
        return floor10 & deep_near_drop & reclaim
    if variant == "box_first_panic_strong_reclaim":
        return floor10 & very_deep_near_drop & strong_reclaim
    if variant == "box_retest_reclaim":
        return retest_base
    if variant == "box_retest_low_amount_reclaim":
        return retest_base & not_overheat_amount
    if variant == "box_retest_capitulation_reclaim":
        return retest_base & capitulation
    raise ValueError(variant)


def prepare_candidates(base: pd.DataFrame, spec: dict[str, str], profile: dict[str, Any]) -> pd.DataFrame:
    part = base[mask_for(base, spec["variant"])].copy()
    if part.empty:
        return part
    part["variant"] = spec["variant"]
    part["desc"] = spec["desc"]
    part["entry_date_ts"] = pd.to_datetime(part["entry_date"], errors="coerce").dt.normalize()
    part["policy_exit_date"] = part["entry_date_ts"].map(exit_dates(part["entry_date_ts"]))
    part["policy_net_ret"] = (
        pd.to_numeric(part["fwd_ret_open_to_close_5d"], errors="coerce")
        - float(profile["cost_bps"]) / 10000.0
        - float(profile["shock"])
    )
    return part.dropna(subset=["entry_date_ts", "policy_exit_date", "policy_net_ret"]).copy()


def source_coverage(base: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for spec in VARIANTS:
        part = base[mask_for(base, spec["variant"])].copy()
        rows.append(
            {
                "variant": spec["variant"],
                "desc": spec["desc"],
                "raw_signals": int(len(part)),
                "signal_days": int(part["entry_date"].nunique()) if len(part) else 0,
                "first_date": part["entry_date"].min() if len(part) else "",
                "last_date": part["entry_date"].max() if len(part) else "",
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = load_base()
    coverage = source_coverage(base)
    coverage.to_csv(OUT_DIR / "coverage.csv", index=False, encoding="utf-8-sig")

    summary_rows: list[dict[str, Any]] = []
    window_rows: list[dict[str, Any]] = []
    for spec in VARIANTS:
        raw = base[mask_for(base, spec["variant"])].copy()
        raw.to_csv(OUT_DIR / f"{spec['variant']}_source_signals.csv", index=False, encoding="utf-8-sig")
        for profile in PROFILES:
            candidates = prepare_candidates(base, spec, profile)
            curve, closed = simulate(candidates)
            run_dir = OUT_DIR / f"{spec['variant']}__{profile['profile']}"
            run_dir.mkdir(parents=True, exist_ok=True)
            candidates.to_csv(run_dir / "candidates.csv", index=False, encoding="utf-8-sig")
            curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
            closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
            summary_rows.append(summarize(curve, closed, spec, str(profile["profile"])))
            window_rows.extend(window_metrics(curve, closed, spec["variant"], str(profile["profile"])))

    summary = pd.DataFrame(summary_rows)
    windows = pd.DataFrame(window_rows)
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "window_metrics.csv", index=False, encoding="utf-8-sig")

    pct_cols = {"total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade", "return"}
    report = [
        "# G3 横盘箱体底部结构化入场源复验 v1",
        "",
        "## 本轮目标",
        "",
        "- 不使用 score/rank 过滤，拆分横盘箱体底部的独立触发源。",
        "- 验证两类方向：第一次恐慌后修复、二次回踩不破后反抽。",
        "- 仍然是研究复验，不接入 G3 实盘。",
        "",
        "## 英文名解释",
        "",
        "- `box_first_panic_reclaim`：箱体底部第一次恐慌后修复。股价在箱体低位，短期出现明显急跌，随后收盘修复或留下承接下影。",
        "- `box_first_panic_strong_reclaim`：更强版本的第一次恐慌后修复。要求跌得更深，并且收盘修复更强。",
        "- `box_retest_reclaim`：箱体底部二次回踩不破后反抽。前面已经从低位反弹过，短线再次回踩但没有破坏箱体底部，收盘重新修复。",
        "- `box_retest_low_amount_reclaim`：二次回踩缩量修复。要求二次回踩不破，同时日线量能不过热，避免追放量冲高。",
        "- `box_retest_capitulation_reclaim`：二次回踩叠加市场出清。二次回踩结构遇到市场恐慌日后修复。",
        "",
        "## 覆盖率",
        "",
        md_table(coverage),
        "",
        "## slot 复算结果",
        "",
        md_table(summary, pct_cols=pct_cols),
        "",
        "## 分窗口稳定性",
        "",
        md_table(windows, pct_cols=pct_cols),
        "",
        "## 初步判断规则",
        "",
        "- 如果某个源只在训练窗口赚钱、验证/盲测亏钱，视为高过拟合风险。",
        "- 如果 cost100 或 shock2 压力下快速转负，说明不适合直接作为高频实盘源。",
        "- 如果二次回踩源比第一次恐慌源更稳，下一步优先接 30m 真承接和失败样本归因。",
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(report), encoding="utf-8")
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()

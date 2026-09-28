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
    exit_dates,
    load_base,
    md_table,
    simulate,
    summarize,
    window_metrics,
)


OUT_DIR = ROOT / "reports" / "gen3_box_first_panic_audit_filters_v1"

VARIANTS = [
    {
        "variant": "strong_panic_base",
        "desc": "箱体底部强修复恐慌基准：箱体极低、近端深跌、收盘强修复",
    },
    {
        "variant": "strong_panic_no_below_low",
        "desc": "排除仍在60日低点下方：避免下跌中继和继续创新低",
    },
    {
        "variant": "strong_panic_no_extreme_amount",
        "desc": "排除20日极端放量：避免恐慌修复变成放量出货",
    },
    {
        "variant": "strong_panic_no_below_low_no_extreme_amount",
        "desc": "同时排除继续创新低和极端放量",
    },
    {
        "variant": "strong_panic_sane_amount_no_below_low",
        "desc": "非创新低且量能适中：20日量能在0.8到1.5之间",
    },
]


def base_mask(df: pd.DataFrame) -> pd.Series:
    floor10 = df["range_pos60"].le(0.10)
    very_deep_near_drop = df["drawdown10"].le(-0.12) | df["drawdown5"].le(-0.08)
    strong_reclaim = df["close_position"].ge(0.68)
    return floor10 & very_deep_near_drop & strong_reclaim


def mask_for(df: pd.DataFrame, variant: str) -> pd.Series:
    m = base_mask(df)
    no_below_low = df["runup_from_60d_low"].ge(0)
    no_extreme_amount = df["amount_ratio20"].le(2.5)
    sane_amount = df["amount_ratio20"].between(0.8, 1.5, inclusive="both")
    if variant == "strong_panic_base":
        return m
    if variant == "strong_panic_no_below_low":
        return m & no_below_low
    if variant == "strong_panic_no_extreme_amount":
        return m & no_extreme_amount
    if variant == "strong_panic_no_below_low_no_extreme_amount":
        return m & no_below_low & no_extreme_amount
    if variant == "strong_panic_sane_amount_no_below_low":
        return m & no_below_low & sane_amount
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


def coverage(base: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for spec in VARIANTS:
        part = base[mask_for(base, spec["variant"])].copy()
        rows.append(
            {
                "variant": spec["variant"],
                "desc": spec["desc"],
                "raw_signals": int(len(part)),
                "signal_days": int(part["entry_date"].nunique()) if len(part) else 0,
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = load_base()
    cov = coverage(base)
    cov.to_csv(OUT_DIR / "coverage.csv", index=False, encoding="utf-8-sig")

    summary_rows: list[dict[str, Any]] = []
    window_rows: list[dict[str, Any]] = []
    for spec in VARIANTS:
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
        "# G3 强修复恐慌审计过滤复验 v1",
        "",
        "## 本轮目标",
        "",
        "- 验证失败归因里看到的结构问题，不做 score/rank 过滤。",
        "- 只测试业务可解释的排除项：继续创新低、极端放量、量能适中。",
        "- 若只改善训练期、不改善验证/盲测，则判定为过拟合风险。",
        "",
        "## 英文名解释",
        "",
        "- `strong_panic_base`：强修复恐慌基准，即 `box_first_panic_strong_reclaim` 的同口径复现。",
        "- `strong_panic_no_below_low`：排除 `runup_from_60d_low < 0`，即还在60日低点下方、可能继续创新低的票。",
        "- `strong_panic_no_extreme_amount`：排除 `amount_ratio20 > 2.5`，即20日量能极端放大的票。",
        "- `strong_panic_no_below_low_no_extreme_amount`：同时排除继续创新低和极端放量。",
        "- `strong_panic_sane_amount_no_below_low`：非创新低，并且20日量能在0.8到1.5之间。",
        "",
        "## 覆盖率",
        "",
        md_table(cov),
        "",
        "## slot 复算结果",
        "",
        md_table(summary, pct_cols=pct_cols),
        "",
        "## 分窗口稳定性",
        "",
        md_table(windows, pct_cols=pct_cols),
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(report), encoding="utf-8")
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()

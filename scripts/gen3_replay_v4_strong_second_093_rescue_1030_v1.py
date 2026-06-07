from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_stress_v4_strong_second_093_execution_v1 import (  # noqa: E402
    PROFILES,
    VARIANT,
    WINDOWS,
    annual_metrics,
    apply_execution_profile,
    diagnostics,
    window_metrics,
)
from scripts.gen3_backtest_strong_volume5_confirm_d3_execution_stress_v1 import _load_daily_prices  # noqa: E402
from scripts.gen3_test_v4_strong_position_scale_v1 import (  # noqa: E402
    INITIAL_CAPITAL,
    VARIANT_ROUTE_LIMITS,
    md_table,
    simulate_scaled,
    summarize,
)


SCALE_DIR = ROOT / "reports" / "gen3_v4_strong_position_scale_probe_v1"
MINUTE_DIR = ROOT / "reports" / "gen3_v4_strong_minute_visibility_audit_v1"
OUT_DIR = ROOT / "reports" / "gen3_v4_strong_second_093_rescue_1030_v1"
RESCUE_VARIANT = "strong_second_score_ge_093_rescue_1030"


def load_rescue_candidates() -> tuple[pd.DataFrame, pd.DataFrame]:
    base = pd.read_csv(SCALE_DIR / "base_candidates.csv", low_memory=False)
    keep = pd.read_csv(SCALE_DIR / "strong_second_score_ge_093_candidates.csv", low_memory=False)
    minute = pd.read_csv(MINUTE_DIR / "minute_visibility_enriched.csv", low_memory=False)

    for d in [base, keep, minute]:
        d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
        d["code"] = d["code"].astype(str)
        for col in ["score", "strong_day_rank", "entry_price", "policy_net_ret", "position_scale", "route_priority"]:
            if col in d.columns:
                d[col] = pd.to_numeric(d[col], errors="coerce")

    keep_keys = set(zip(keep["entry_date"], keep["code"]))
    rescue_keys = set(
        zip(
            minute[
                minute["group"].astype(str).eq("skipped_by_093")
                & minute["m30_visible_strength_1030"].fillna(False).astype(bool)
            ]["entry_date"],
            minute[
                minute["group"].astype(str).eq("skipped_by_093")
                & minute["m30_visible_strength_1030"].fillna(False).astype(bool)
            ]["code"],
        )
    )

    d = base.copy()
    d["position_scale"] = d.get("position_scale", 1.0).fillna(1.0)
    d["rescue_1030"] = False
    d["rescue_note"] = "baseline_093_keep_or_non_strong"
    keys = list(zip(d["entry_date"], d["code"]))
    keep_mask = pd.Series([key in keep_keys for key in keys], index=d.index)
    rescue_mask = pd.Series([key in rescue_keys for key in keys], index=d.index)
    d = d[keep_mask | rescue_mask].copy()
    d.loc[rescue_mask.reindex(d.index).fillna(False), "rescue_1030"] = True
    d.loc[d["rescue_1030"], "rescue_note"] = "rank2_score_lt093_m30_strength_1030"
    d["scale_variant"] = RESCUE_VARIANT
    d = d.dropna(subset=["entry_date", "policy_exit_date", "entry_price", "policy_net_ret", "code"])
    d["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
    d = d.sort_values(["entry_date", "route_priority", "score"], ascending=[True, False, False]).reset_index(drop=True)

    rescued = d[d["rescue_1030"]].copy()
    return d, rescued


def write_report(summary: pd.DataFrame, windows: pd.DataFrame, annual: pd.DataFrame, rescued: pd.DataFrame, diagnostics_df: pd.DataFrame) -> None:
    pct_cols = {
        "total_return",
        "max_drawdown",
        "win_rate",
        "avg_trade_return",
        "worst_trade",
        "worst_open_mtm_ret",
        "strong_main_avg_ret",
        "return",
        "mean_candidate_ret",
        "worst_candidate_ret",
        "bad10_candidate_rate",
    }
    lines = [
        "# G3 V4 strong_second_score_ge_093 + 10:30 二号票救回回放 v1",
        "",
        "## 边界",
        "",
        "- 固定基线为 `strong_second_score_ge_093`。",
        "- 只救回 `strong_day_rank >= 2 且 score < 0.93`，并且真实 30m `10:30` 可见强度通过的 strong_main。",
        "- 不调 `0.93`，不调 10:30 强度阈值，不新增其他日线过滤。",
        "- 这是研究回放，不进入实盘，不进入 G3 正式组合。",
        "",
        "## 救回样本",
        "",
        md_table(
            rescued[["entry_date", "code", "name", "score", "strong_day_rank", "policy_net_ret", "rescue_note"]],
            pct_cols={"policy_net_ret"},
        ),
        "",
        "## Full 汇总",
        "",
        md_table(summary, pct_cols=pct_cols),
        "",
        "## 窗口复核",
        "",
        md_table(windows, pct_cols=pct_cols),
        "",
        "## 年度复核",
        "",
        md_table(annual, pct_cols=pct_cols),
        "",
        "## 执行诊断",
        "",
        md_table(diagnostics_df, pct_cols=pct_cols),
        "",
        "## 判断",
        "",
        "- 这条规则若只提升 close 口径、但 nextopen/haircut2 不改善，则只能证明二号票进攻有效，不能证明 G3 可实盘。",
        "- 若 train/valid/blind 都有正贡献，且 nextopen 口径改善明显，才值得进入下一轮 5m/板块主线复核。",
        "- 若收益主要来自 2024/2026 少数票，则视为样本线索，不升级规则。",
        "",
    ]
    (OUT_DIR / "report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    candidates, rescued = load_rescue_candidates()
    candidates.to_csv(OUT_DIR / "rescue_1030_candidates.csv", index=False, encoding="utf-8-sig")
    rescued.to_csv(OUT_DIR / "rescued_trades.csv", index=False, encoding="utf-8-sig")
    daily = _load_daily_prices(candidates, extra_days=20)

    summary_rows: list[dict[str, Any]] = []
    window_rows: list[dict[str, Any]] = []
    annual_rows: list[dict[str, Any]] = []
    diag_rows: list[dict[str, Any]] = []

    for profile in PROFILES:
        name = str(profile["profile"])
        stressed = apply_execution_profile(candidates, daily, profile)
        run_dir = OUT_DIR / name
        run_dir.mkdir(parents=True, exist_ok=True)
        stressed.to_csv(run_dir / "candidates.csv", index=False, encoding="utf-8-sig")
        curve, closed = simulate_scaled(stressed, float(profile["cost_bps"]), VARIANT_ROUTE_LIMITS.get(VARIANT))
        curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
        closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
        summary_rows.append(summarize(curve, closed, RESCUE_VARIANT, name))
        window_rows.extend(window_metrics(curve, closed, name))
        annual_rows.extend(annual_metrics(curve, closed, name))
        diag_rows.append(diagnostics(stressed, name))

    summary = pd.DataFrame(summary_rows)
    windows = pd.DataFrame(window_rows)
    annual = pd.DataFrame(annual_rows)
    diagnostics_df = pd.DataFrame(diag_rows)
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "window_metrics.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(OUT_DIR / "annual_metrics.csv", index=False, encoding="utf-8-sig")
    diagnostics_df.to_csv(OUT_DIR / "diagnostics.csv", index=False, encoding="utf-8-sig")
    write_report(summary, windows, annual, rescued, diagnostics_df)
    print(f"wrote {OUT_DIR}")
    print(f"rescued={len(rescued)} candidates={len(candidates)} initial_capital={INITIAL_CAPITAL:.0f}")
    print(summary[["profile", "trade_count", "total_return", "max_drawdown", "win_rate", "strong_main_pnl"]].to_string(index=False))


if __name__ == "__main__":
    main()

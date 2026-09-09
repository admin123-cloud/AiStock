from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


from pathlib import Path
import sys
from typing import Any

import pandas as pd


ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_test_range_reversal_gate_d1d2_exit_v1 import (
    PROFILES,
    apply_policy,
    load_base,
    md_table,
    simulate,
    summarize,
    window_metrics,
)


OUT_DIR = _report_path() / "gen3_range_bad_reversal_combo_v1"

VARIANTS = [
    {
        "variant": "baseline_all",
        "desc": "原始 ice_recent3_base 全样本",
        "mode": "all",
    },
    {
        "variant": "bad_reversal_only",
        "desc": "只买亏损型警戒候选：冲高回落警戒 + 30m量能不足 + 日线修复不足",
        "mode": "only_bad",
    },
    {
        "variant": "drop_bad_reversal",
        "desc": "剔除亏损型警戒候选",
        "mode": "drop_bad",
    },
]

POLICY = {
    "policy": "hold5_baseline",
    "desc": "入场后默认持有5日收盘",
    "skip_warning": False,
    "warn_d1exit": False,
    "warn_d2exit": False,
    "warn_exit_if_weak": False,
    "weak_d1d2_exit": False,
}


def pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def select_variant(base: pd.DataFrame, mode: str) -> pd.DataFrame:
    bad = (
        base["entry_reversal_warning"].fillna(False)
        & pd.to_numeric(base["amount_ratio3"], errors="coerce").lt(2.0)
        & pd.to_numeric(base["close_position"], errors="coerce").lt(0.60)
    )
    out = base.copy()
    out["bad_reversal_combo"] = bad
    if mode == "all":
        return out
    if mode == "only_bad":
        return out[bad].copy()
    if mode == "drop_bad":
        return out[~bad].copy()
    raise ValueError(mode)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = load_base()
    summary_rows: list[dict[str, Any]] = []
    window_rows: list[dict[str, Any]] = []
    coverage_rows: list[dict[str, Any]] = []

    for spec in VARIANTS:
        selected = select_variant(base, spec["mode"])
        selected["source_variant"] = spec["variant"]
        selected["source_desc"] = spec["desc"]
        selected.to_csv(OUT_DIR / f"{spec['variant']}_signals.csv", index=False, encoding="utf-8-sig")
        coverage_rows.append(
            {
                "variant": spec["variant"],
                "desc": spec["desc"],
                "signals": int(len(selected)),
                "bad_reversal_combo_count": int(selected["bad_reversal_combo"].sum()) if len(selected) else 0,
            }
        )
        for profile in PROFILES:
            run = apply_policy(selected, POLICY, profile)
            curve, closed = simulate(run)
            run_dir = OUT_DIR / f"{spec['variant']}__{profile['profile']}"
            run_dir.mkdir(parents=True, exist_ok=True)
            run.to_csv(run_dir / "candidates.csv", index=False, encoding="utf-8-sig")
            curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
            closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
            summary_rows.append(
                summarize(
                    curve,
                    closed,
                    spec["variant"],
                    POLICY["policy"],
                    profile["profile"],
                    spec["desc"],
                    POLICY["desc"],
                )
            )
            window_rows.extend(window_metrics(curve, closed, spec["variant"], POLICY["policy"], profile["profile"]))

    summary = pd.DataFrame(summary_rows)
    windows = pd.DataFrame(window_rows)
    coverage = pd.DataFrame(coverage_rows)
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "window_metrics.csv", index=False, encoding="utf-8-sig")
    coverage.to_csv(OUT_DIR / "coverage.csv", index=False, encoding="utf-8-sig")

    pct_cols = {"total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade", "return"}
    report = [
        "# G3 range 亏损型冲高回落组合复验 v1",
        "",
        "## 本轮结论口径",
        "",
        "- 只验证一个窄组合，不做参数搜索。",
        "- 组合为 `bad_reversal_combo = entry_reversal_warning + low_amount3 + daily_reclaim_weak`。",
        "- 如果 `drop_bad_reversal` 对 full、valid、blind 和压力口径没有稳定改善，则这条卖出/过滤链路停止。",
        "",
        "## 英文名解释",
        "",
        "- `entry_reversal_warning`：入场日冲高回落/突破失败警戒。",
        "- `low_amount3`：30m确认量能相对前三根均量小于2倍，代表承接量不足。",
        "- `daily_reclaim_weak`：日线收盘修复位置低于60%，代表日线修复不够强。",
        "- `bad_reversal_combo`：上述三者同时出现的“亏损型警戒组合”。",
        "- `baseline_all`：原始全样本。",
        "- `bad_reversal_only`：只买亏损型警戒组合，用来验证它是否真的差。",
        "- `drop_bad_reversal`：剔除亏损型警戒组合，用来验证是否能改善策略。",
        "",
        "## 覆盖率",
        "",
        md_table(coverage),
        "",
        "## slot复算结果",
        "",
        md_table(summary, pct_cols=pct_cols),
        "",
        "## 分窗口结果",
        "",
        md_table(windows, pct_cols=pct_cols),
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(report), encoding="utf-8")
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()

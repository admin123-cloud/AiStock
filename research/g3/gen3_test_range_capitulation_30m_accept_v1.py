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

from scripts.gen3_test_range_30m_volume_acceptance_v1 import (  # noqa: E402
    PROFILES,
    build_30m_acceptance,
    load_base,
    md_table,
    standardize,
    summarize,
    window_metrics,
)
from scripts.gen3_test_range_30m_volume_acceptance_v1 import simulate as simulate_30m  # noqa: E402
from scripts.gen3_rebuild_range_box_source_map_v1 import (  # noqa: E402
    prepare_candidates,
    simulate as simulate_daily,
    summarize as summarize_daily,
    window_metrics as window_metrics_daily,
)


OUT_DIR = _report_path() / "gen3_range_capitulation_30m_accept_v1"


def daily_mask(df: pd.DataFrame) -> pd.Series:
    return (
        df["g3_chain"].eq("range_box_bottom")
        & pd.to_numeric(df["range_pos60"], errors="coerce").le(0.10)
        & pd.to_numeric(df["big_down_rate"], errors="coerce").ge(0.10)
        & pd.to_numeric(df["close_position"], errors="coerce").ge(0.60)
    )


def pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = load_base()
    daily = base[daily_mask(base)].copy()
    daily["variant"] = "floor10_capitulation_reclaim_daily"
    daily["desc"] = "箱体位置低于10%、市场有大跌出清、日线收盘修复高于60%的日线结构源"
    daily["family"] = "range_capitulation_reclaim"
    daily["hold_days"] = 5
    daily.to_csv(OUT_DIR / "daily_source_signals.csv", index=False, encoding="utf-8-sig")

    m30 = build_30m_acceptance(daily)
    if not m30.empty:
        m30["variant"] = "floor10_capitulation_reclaim_30m_accept"
        m30["desc"] = "日线出清修复结构源叠加30m真实放量承接确认"
        m30["family"] = "range_capitulation_reclaim_30m_accept"
        m30["hold_days"] = 5
        m30["rank_key"] = pd.to_numeric(m30.get("amount_ratio3", 0.0), errors="coerce").fillna(0.0)
        m30["rank_in_day"] = m30.groupby("entry_date")["rank_key"].rank(method="first", ascending=False)
        m30 = m30[m30["rank_in_day"].le(1)].copy()
    m30.to_csv(OUT_DIR / "m30_accept_signals.csv", index=False, encoding="utf-8-sig")

    summary_rows: list[dict[str, Any]] = []
    window_rows: list[dict[str, Any]] = []
    coverage_rows = [
        {
            "variant": "floor10_capitulation_reclaim_daily",
            "desc": "日线结构源",
            "raw_signals": int(len(daily)),
            "signal_days": int(daily["entry_date"].nunique()) if len(daily) else 0,
        },
        {
            "variant": "floor10_capitulation_reclaim_30m_accept",
            "desc": "日线结构源 + 30m真实承接",
            "raw_signals": int(len(m30)),
            "signal_days": int(m30["entry_date"].nunique()) if len(m30) else 0,
        },
    ]

    daily_spec = {
        "variant": "floor10_capitulation_reclaim_daily",
        "desc": "日线结构源：箱体低位 + 市场出清 + 日线修复",
    }
    daily_spec_for_prepare = dict(daily_spec)
    daily_spec_for_prepare["variant"] = "floor10_capitulation_reclaim"
    for profile in PROFILES:
        daily_candidates = prepare_candidates(base, daily_spec_for_prepare, profile)
        daily_candidates = daily_candidates[daily_mask(daily_candidates)].copy()
        daily_candidates["variant"] = daily_spec["variant"]
        daily_candidates["desc"] = daily_spec["desc"]
        if "struct_tiebreak" not in daily_candidates.columns:
            daily_candidates["struct_tiebreak"] = (
                (1.0 - pd.to_numeric(daily_candidates["range_pos60"], errors="coerce").clip(0, 1)).fillna(0.0) * 10.0
                + pd.to_numeric(daily_candidates["close_position"], errors="coerce").clip(0, 1).fillna(0.0)
                + pd.to_numeric(daily_candidates["amount_ratio20"], errors="coerce").clip(0, 3).fillna(0.0) * 0.05
            )
        daily_curve, daily_closed = simulate_daily(daily_candidates)
        daily_run_dir = OUT_DIR / f"daily__{profile['profile']}"
        daily_run_dir.mkdir(parents=True, exist_ok=True)
        daily_candidates.to_csv(daily_run_dir / "candidates.csv", index=False, encoding="utf-8-sig")
        daily_curve.to_csv(daily_run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
        daily_closed.to_csv(daily_run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
        summary_rows.append(summarize_daily(daily_curve, daily_closed, daily_spec, str(profile["profile"])))
        window_rows.extend(window_metrics_daily(daily_curve, daily_closed, daily_spec["variant"], str(profile["profile"])))

        m30_candidates = standardize(m30, profile) if not m30.empty else pd.DataFrame()
        m30_curve, m30_closed = simulate_30m(m30_candidates)
        m30_run_dir = OUT_DIR / f"m30_accept__{profile['profile']}"
        m30_run_dir.mkdir(parents=True, exist_ok=True)
        m30_candidates.to_csv(m30_run_dir / "candidates.csv", index=False, encoding="utf-8-sig")
        m30_curve.to_csv(m30_run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
        m30_closed.to_csv(m30_run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
        summary_rows.append(
            summarize(
                m30_curve,
                m30_closed,
                "floor10_capitulation_reclaim_30m_accept",
                str(profile["profile"]),
                "日线出清修复结构源叠加30m真实放量承接确认",
            )
        )
        window_rows.extend(window_metrics(m30_curve, m30_closed, "floor10_capitulation_reclaim_30m_accept", str(profile["profile"])))

    summary = pd.DataFrame(summary_rows)
    windows = pd.DataFrame(window_rows)
    coverage = pd.DataFrame(coverage_rows)
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "window_metrics.csv", index=False, encoding="utf-8-sig")
    coverage.to_csv(OUT_DIR / "coverage.csv", index=False, encoding="utf-8-sig")

    pct_cols = {"total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade", "return", "confirm_rate"}
    report = [
        "# G3 range 出清修复源 + 30m真实承接复验 v1",
        "",
        "## 本轮目标",
        "",
        "- 验证 `floor10_capitulation_reclaim` 是否需要叠加 30m 真实承接确认。",
        "- 不使用 `score/rank` 做过滤，只用日线结构源和固定 30m 承接定义。",
        "- 这是研究复验，不接入 G3 实盘。",
        "",
        "## 英文名解释",
        "",
        "- `floor10_capitulation_reclaim_daily`：日线结构源。箱体位置低于10%、市场有大跌出清、个股日线收盘修复高于60%。",
        "- `floor10_capitulation_reclaim_30m_accept`：在上述日线结构源上，叠加 30m 放量承接确认后才买。",
        "- `cost30`：约30bps交易摩擦。",
        "- `cost100`：约100bps高摩擦压力。",
        "- `shock2_cost30`：30bps成本基础上额外扣2%，模拟低开、滑点和成交冲击。",
        "",
        "## 覆盖率",
        "",
        md_table(coverage),
        "",
        "## slot复算结果",
        "",
        md_table(summary, pct_cols=pct_cols),
        "",
        "## 分窗口稳定性",
        "",
        md_table(windows, pct_cols=pct_cols),
        "",
        "## 下一步判断",
        "",
        "- 若 30m 承接能改善 2026YTD 和 shock2，则继续做 30m 失败样本归因。",
        "- 若 30m 承接减少交易但没有改善压力口径，则说明问题不是承接确认，而是日线源本身仍不够独立。",
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(report), encoding="utf-8")
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()

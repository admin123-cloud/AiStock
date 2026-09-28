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

from scripts.gen3_test_range_30m_volume_acceptance_v1 import PROFILES, simulate, summarize, window_metrics  # noqa: E402
from scripts.gen3_test_range_box_first_panic_distill_second_accept_v1 import (  # noqa: E402
    concentration,
    group_year,
    md_table,
    standardize_policy,
)


SOURCE = _report_path() / "gen3_range_box_first_panic_distill_second_accept_v1" / "base_with_second_acceptance_flags.csv"
OUT_DIR = _report_path() / "gen3_range_first_panic_d1_accept_stability_v1"
SIGNAL_START = "2020-01-01"
SIGNAL_END = "2026-05-29"
CURVE_END = "2026-06-04"

VARIANTS = [
    {
        "variant": "d1_second",
        "desc_cn": "第一次恐慌修复，D1出现二次30m承接",
        "mode": "d1",
    },
    {
        "variant": "d1_reclaim60",
        "desc_cn": "D1二次30m承接，且日线修复>=60%",
        "mode": "d1_reclaim60",
    },
    {
        "variant": "d1_reclaim60_amt12",
        "desc_cn": "D1二次30m承接，日线修复>=60%，日线量能不过热(amount_ratio20<=1.2)",
        "mode": "d1_reclaim60_amt12",
    },
    {
        "variant": "d1_reclaim60_amt15",
        "desc_cn": "D1二次30m承接，日线修复>=60%，日线量能不过热(amount_ratio20<=1.5)",
        "mode": "d1_reclaim60_amt15",
    },
    {
        "variant": "d1_reclaim55_amt15",
        "desc_cn": "D1二次30m承接，日线修复>=55%，日线量能不过热(amount_ratio20<=1.5)",
        "mode": "d1_reclaim55_amt15",
    },
    {
        "variant": "d1_reclaim50_amt15",
        "desc_cn": "D1二次30m承接，日线修复>=50%，日线量能不过热(amount_ratio20<=1.5)",
        "mode": "d1_reclaim50_amt15",
    },
    {
        "variant": "any_reclaim60_amt15",
        "desc_cn": "D0/D1任一二次30m承接，日线修复>=60%，日线量能不过热(amount_ratio20<=1.5)",
        "mode": "any_reclaim60_amt15",
    },
    {
        "variant": "d1_reclaim55_pressure05",
        "desc_cn": "D1二次30m承接，日线修复>=55%，市场大跌比例5%-35%",
        "mode": "d1_reclaim55_pressure05",
    },
]


def pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return "--"
    return f"{float(value) * 100:+.2f}%"


def load_base() -> pd.DataFrame:
    d = pd.read_csv(SOURCE, low_memory=False)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    for col in [
        "close_position",
        "amount_ratio20",
        "big_down_rate",
        "drawdown10",
        "range_pos60",
        "fwd_ret_confirm_to_close_3d",
        "fwd_ret_confirm_to_close_5d",
        "entry_price_adjusted",
    ]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    for col in ["d0_second_accept", "d1_second_accept"]:
        d[col] = d[col].astype(bool)
    d["hold_days"] = 5
    d["rank_key"] = pd.to_numeric(d.get("struct_tiebreak", 0.0), errors="coerce").fillna(0.0)
    return d.dropna(subset=["entry_date", "code", "entry_price_adjusted", "fwd_ret_confirm_to_close_3d", "fwd_ret_confirm_to_close_5d"]).copy()


def mask_for(base: pd.DataFrame, mode: str) -> pd.Series:
    d1 = base["d1_second_accept"]
    any_second = base["d0_second_accept"] | base["d1_second_accept"]
    if mode == "d1":
        return d1
    if mode == "d1_reclaim60":
        return d1 & base["close_position"].ge(0.60)
    if mode == "d1_reclaim60_amt12":
        return d1 & base["close_position"].ge(0.60) & base["amount_ratio20"].le(1.20)
    if mode == "d1_reclaim60_amt15":
        return d1 & base["close_position"].ge(0.60) & base["amount_ratio20"].le(1.50)
    if mode == "d1_reclaim55_amt15":
        return d1 & base["close_position"].ge(0.55) & base["amount_ratio20"].le(1.50)
    if mode == "d1_reclaim50_amt15":
        return d1 & base["close_position"].ge(0.50) & base["amount_ratio20"].le(1.50)
    if mode == "any_reclaim60_amt15":
        return any_second & base["close_position"].ge(0.60) & base["amount_ratio20"].le(1.50)
    if mode == "d1_reclaim55_pressure05":
        return d1 & base["close_position"].ge(0.55) & base["big_down_rate"].between(0.05, 0.35, inclusive="left")
    raise ValueError(mode)


def coverage(base: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for spec in VARIANTS:
        d = base[mask_for(base, spec["mode"])].copy()
        ret = pd.to_numeric(d["fwd_ret_confirm_to_close_5d"], errors="coerce") - 0.003
        rows.append(
            {
                "variant": spec["variant"],
                "desc": spec["desc_cn"],
                "signal_count": int(len(d)),
                "signal_days": int(d["entry_date"].nunique()) if len(d) else 0,
                "raw_cost30_avg_5d": float(ret.mean()) if len(ret) else 0.0,
                "raw_cost30_win_5d": float((ret > 0).mean()) if len(ret) else 0.0,
                "median_close_position": float(d["close_position"].median()) if len(d) else 0.0,
                "median_amount_ratio20": float(d["amount_ratio20"].median()) if len(d) else 0.0,
                "avg_big_down_rate": float(d["big_down_rate"].mean()) if len(d) else 0.0,
            }
        )
    return pd.DataFrame(rows)


def write_report(
    cov: pd.DataFrame,
    summary: pd.DataFrame,
    windows: pd.DataFrame,
    best_variant: str,
    best_desc: str,
    yearly: pd.DataFrame,
    conc: pd.DataFrame,
) -> None:
    pct_cols = {
        "raw_cost30_avg_5d",
        "raw_cost30_win_5d",
        "median_close_position",
        "median_amount_ratio20",
        "avg_big_down_rate",
        "total_return",
        "max_drawdown",
        "win_rate",
        "avg_trade_return",
        "worst_trade",
        "return",
        "avg_return",
        "worst_return",
        "best_return",
        "remaining_pnl_share",
        "remaining_avg_return",
        "remaining_win_rate",
    }
    best_summary = summary[summary["variant"].eq(best_variant)].copy()
    best_windows = windows[windows["variant"].eq(best_variant)].copy()
    lines = [
        "# G3 v4 D1二次承接稳定性矩阵",
        "",
        "## 回测范围",
        f"- 信号入场窗口：{SIGNAL_START} 至 {SIGNAL_END}。",
        f"- 曲线结算窗口：{SIGNAL_START} 至 {CURVE_END}。",
        "- 初始资金：150000；5个槽位；单槽20%；同日最多开1笔。",
        "- 本轮只用结构条件，不用新的score/rank过滤；目标是从219笔收缩到80-120笔附近。",
        "",
        "## 英文策略名解释",
        "- `d1_second`：D1二次30m承接。首个30m确认后的下一个交易日，再出现一次放量且收在高位的30m承接。",
        "- `reclaim60/reclaim55/reclaim50`：日线收盘修复到当天振幅区间的60%/55%/50%以上。",
        "- `amt12/amt15`：日线量能不过热，`amount_ratio20 <= 1.2/1.5`，避免追过热放量。",
        "- `pressure05`：市场大跌比例在5%-35%之间，表示有压力释放但不是极端崩盘。",
        "- 所有方案统一使用 `d3neg` 退出：D3仍亏就D3退出，否则D5退出。",
        "",
        "## 覆盖率",
        md_table(cov, pct_cols=pct_cols),
        "",
        "## slot复算结果",
        md_table(summary, pct_cols=pct_cols),
        "",
        f"## 当前相对最好方案：`{best_variant}`",
        f"- 中文含义：{best_desc}",
        md_table(best_summary, pct_cols=pct_cols),
        "",
        "## 最好方案分窗口",
        md_table(best_windows, pct_cols=pct_cols),
        "",
        "## 最好方案年度拆分（cost30）",
        md_table(yearly, pct_cols=pct_cols, money_cols={"pnl"}),
        "",
        "## 最好方案集中度（cost30）",
        md_table(conc, pct_cols=pct_cols, money_cols={"remaining_pnl"}),
        "",
        "## 判断",
        "- 若80-120笔候选包在2024-2025和2%冲击口径仍为正，说明横盘源可以继续研究为候选包。",
        "- 若全周期靠少数大票或2026显著转弱，仍不能进入G3实盘。",
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = load_base()
    cov = coverage(base)
    cov.to_csv(OUT_DIR / "coverage.csv", index=False, encoding="utf-8-sig")
    summary_rows: list[dict[str, Any]] = []
    window_rows: list[dict[str, Any]] = []
    closed_map: dict[tuple[str, str], pd.DataFrame] = {}
    best_variant = ""
    best_desc = ""
    best_key: tuple[float, float, float, int] | None = None
    for spec in VARIANTS:
        selected = base[mask_for(base, spec["mode"])].copy()
        selected.to_csv(OUT_DIR / f"{spec['variant']}_signals.csv", index=False, encoding="utf-8-sig")
        variant = f"{spec['variant']}__d3neg"
        desc = f"{spec['desc_cn']}；D3仍为负则D3退出，否则D5退出"
        for profile in PROFILES:
            candidates = standardize_policy(selected, profile, variant, desc, "d3neg")
            curve, closed = simulate(candidates)
            run_dir = OUT_DIR / f"{variant}__{profile['profile']}"
            run_dir.mkdir(parents=True, exist_ok=True)
            candidates.to_csv(run_dir / "candidates.csv", index=False, encoding="utf-8-sig")
            curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
            closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
            closed_map[(variant, str(profile["profile"]))] = closed
            summary_rows.append(summarize(curve, closed, variant, str(profile["profile"]), desc))
            window_rows.extend(window_metrics(curve, closed, variant, str(profile["profile"])))
        cost30 = [r for r in summary_rows if r["variant"] == variant and r["profile"] == "cost30"][-1]
        shock = [r for r in summary_rows if r["variant"] == variant and r["profile"] == "shock2_cost30"][-1]
        n = int(cost30.get("trade_count", 0))
        if 60 <= n <= 140:
            key = (
                float(shock.get("total_return", 0.0)),
                float(cost30.get("total_return", 0.0)),
                -abs(float(cost30.get("max_drawdown", 0.0))),
                n,
            )
            if best_key is None or key > best_key:
                best_key = key
                best_variant = variant
                best_desc = desc
    summary = pd.DataFrame(summary_rows)
    windows = pd.DataFrame(window_rows)
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "window_metrics.csv", index=False, encoding="utf-8-sig")
    if not best_variant:
        row = summary[summary["profile"].eq("cost30")].sort_values("total_return", ascending=False).iloc[0]
        best_variant = str(row["variant"])
        best_desc = str(row["desc"])
    best_closed = closed_map[(best_variant, "cost30")]
    yearly = group_year(best_closed)
    conc = concentration(best_closed)
    yearly.to_csv(OUT_DIR / "best_yearly_cost30.csv", index=False, encoding="utf-8-sig")
    conc.to_csv(OUT_DIR / "best_concentration_cost30.csv", index=False, encoding="utf-8-sig")
    keep_cols = [
        "entry_date",
        "policy_exit_date",
        "code",
        "name",
        "exit_reason",
        "policy_net_ret",
        "realized_pnl",
        "range_pos60",
        "drawdown10",
        "close_position",
        "big_down_rate",
        "amount_ratio20",
        "d0_second_accept",
        "d1_second_accept",
        "fwd_ret_confirm_to_close_3d",
        "fwd_ret_confirm_to_close_5d",
    ]
    keep_cols = [c for c in keep_cols if c in best_closed.columns]
    best_closed.sort_values("realized_pnl", ascending=False).head(20)[keep_cols].to_csv(
        OUT_DIR / "best_top_trades_cost30.csv", index=False, encoding="utf-8-sig"
    )
    best_closed.sort_values("policy_net_ret").head(20)[keep_cols].to_csv(
        OUT_DIR / "best_worst_trades_cost30.csv", index=False, encoding="utf-8-sig"
    )
    valid = best_closed[
        pd.to_datetime(best_closed["entry_date"], errors="coerce").between(pd.Timestamp("2024-01-01"), pd.Timestamp("2025-12-31"))
    ].copy()
    valid.sort_values("entry_date")[keep_cols].to_csv(OUT_DIR / "best_valid_2024_2025_trades_cost30.csv", index=False, encoding="utf-8-sig")
    write_report(cov, summary, windows, best_variant, best_desc, yearly, conc)
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()

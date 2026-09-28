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

from utils.paths import report_path

ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_test_range_30m_volume_acceptance_v1 import PROFILES, standardize, window_metrics  # noqa: E402
from scripts.gen3_test_range_box_stress_icepoint_climax_overlay_v1 import md_table, simulate_scaled, summarize  # noqa: E402


SOURCE_DIR = report_path("gen3_range_second_acceptance_v1")
OUT_DIR = report_path("gen3_range_second_acceptance_structure_veto_v1")

BACKTEST_START = "2020-01-01"
BACKTEST_END = "2026-05-29"
CURVE_END = "2026-06-04"

SOURCES = [
    {
        "source_variant": "deep_and_reclaim_d1_second_accept",
        "source_file": "deep_and_reclaim_d1_second_accept_signals.csv",
        "source_desc": "箱体底部且日线修复，并要求D1再次放量承接",
    },
    {
        "source_variant": "deep_and_reclaim_any_second_accept",
        "source_file": "deep_and_reclaim_any_second_accept_signals.csv",
        "source_desc": "箱体底部且日线修复，并要求D0后半日或D1任一再次放量承接",
    },
]

RULES = [
    {
        "rule": "base",
        "desc": "原始候选源，不加结构否决",
    },
    {
        "rule": "not_floor_not_strong_veto",
        "desc": "否决既不够贴近60日底部，也没有日线强修复的样本",
    },
    {
        "rule": "not_floor_not_strong_unless_extreme_volume",
        "desc": "否决既不够低也不够强，但允许30m极端放量承接",
    },
]


def pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def load_signals(path: Path) -> pd.DataFrame:
    d = pd.read_csv(path, low_memory=False)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["code"] = d["code"].astype(str)
    for col in [
        "range_pos60",
        "close_position",
        "amount_ratio3",
        "candidate_score",
        "rank_key",
        "fwd_ret_confirm_to_close_5d",
    ]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    d["near_floor"] = d["range_pos60"].le(0.03)
    d["strong_reclaim"] = d["close_position"].ge(0.75)
    d["extreme_accept"] = d["amount_ratio3"].ge(3.0)
    d["structure_veto"] = (~d["near_floor"]) & (~d["strong_reclaim"])
    d["structure_veto_unless_extreme"] = d["structure_veto"] & (~d["extreme_accept"])
    return d.dropna(subset=["entry_date", "code", "entry_price_adjusted", "fwd_ret_confirm_to_close_5d"]).copy()


def apply_rule(d: pd.DataFrame, rule: str) -> pd.DataFrame:
    if rule == "base":
        return d.copy()
    if rule == "not_floor_not_strong_veto":
        return d[~d["structure_veto"]].copy()
    if rule == "not_floor_not_strong_unless_extreme_volume":
        return d[~d["structure_veto_unless_extreme"]].copy()
    raise ValueError(rule)


def signal_coverage(source: str, rule: str, base: pd.DataFrame, selected: pd.DataFrame) -> dict[str, Any]:
    raw_ret = pd.to_numeric(selected.get("fwd_ret_confirm_to_close_5d", pd.Series(dtype=float)), errors="coerce")
    return {
        "source_variant": source,
        "rule": rule,
        "signal_count": int(len(selected)),
        "signal_days": int(selected["entry_date"].nunique()) if len(selected) else 0,
        "removed_count": int(len(base) - len(selected)),
        "removed_loss_count": int((base[~base.index.isin(selected.index)]["fwd_ret_confirm_to_close_5d"] < 0).sum()) if len(base) else 0,
        "raw_avg_5d": float(raw_ret.mean()) if len(raw_ret) else 0.0,
        "raw_win_rate": float((raw_ret > 0).mean()) if len(raw_ret) else 0.0,
    }


def top_exclusion(closed: pd.DataFrame, n: int) -> dict[str, Any]:
    if closed.empty:
        return {"remaining_trades": 0, "remaining_pnl": 0.0, "remaining_avg_ret": 0.0, "remaining_win_rate": 0.0}
    d = closed.copy()
    d["realized_pnl"] = pd.to_numeric(d["realized_pnl"], errors="coerce")
    d["policy_net_ret"] = pd.to_numeric(d["policy_net_ret"], errors="coerce")
    tail = d.sort_values("realized_pnl", ascending=False).iloc[n:].copy()
    return {
        "remaining_trades": int(len(tail)),
        "remaining_pnl": float(tail["realized_pnl"].sum()) if len(tail) else 0.0,
        "remaining_avg_ret": float(tail["policy_net_ret"].mean()) if len(tail) else 0.0,
        "remaining_win_rate": float((tail["policy_net_ret"] > 0).mean()) if len(tail) else 0.0,
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    summary_rows: list[dict[str, Any]] = []
    window_rows: list[dict[str, Any]] = []
    coverage_rows: list[dict[str, Any]] = []
    concentration_rows: list[dict[str, Any]] = []

    for source in SOURCES:
        source_variant = source["source_variant"]
        base = load_signals(SOURCE_DIR / source["source_file"])
        for rule in RULES:
            selected = apply_rule(base, rule["rule"])
            selected = selected.copy()
            selected["variant"] = f"{source_variant}__{rule['rule']}"
            selected["desc"] = f"{source['source_desc']}；{rule['desc']}"
            selected["family"] = "range_second_acceptance_structure_veto"
            selected["hold_days"] = 5
            selected["rank_key"] = pd.to_numeric(selected.get("rank_key", selected.get("candidate_score", 0.0)), errors="coerce").fillna(0.0)
            selected["rank_in_day"] = selected.groupby("entry_date")["rank_key"].rank(method="first", ascending=False)
            selected.to_csv(OUT_DIR / f"{source_variant}__{rule['rule']}_signals.csv", index=False, encoding="utf-8-sig")
            coverage_rows.append(signal_coverage(source_variant, rule["rule"], base, selected))

            for profile in PROFILES:
                candidates = standardize(selected, profile)
                curve, closed = simulate_scaled(candidates)
                run_dir = OUT_DIR / f"{source_variant}__{rule['rule']}__{profile['profile']}"
                run_dir.mkdir(parents=True, exist_ok=True)
                candidates.to_csv(run_dir / "candidates.csv", index=False, encoding="utf-8-sig")
                curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
                closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
                label = f"{source_variant}__{rule['rule']}"
                row = summarize(curve, closed, label, str(profile["profile"]), str(rule["desc"]))
                row["source_variant"] = source_variant
                row["rule"] = rule["rule"]
                summary_rows.append(row)
                window_rows.extend(window_metrics(curve, closed, label, str(profile["profile"])))
                if profile["profile"] == "cost30":
                    ex1 = top_exclusion(closed, 1)
                    ex3 = top_exclusion(closed, 3)
                    concentration_rows.append(
                        {
                            "source_variant": source_variant,
                            "rule": rule["rule"],
                            "trade_count": int(len(closed)),
                            "total_pnl": float(pd.to_numeric(closed.get("realized_pnl", pd.Series(dtype=float)), errors="coerce").sum())
                            if not closed.empty
                            else 0.0,
                            "top1_removed_trades": ex1["remaining_trades"],
                            "top1_removed_pnl": ex1["remaining_pnl"],
                            "top1_removed_avg_ret": ex1["remaining_avg_ret"],
                            "top1_removed_win_rate": ex1["remaining_win_rate"],
                            "top3_removed_trades": ex3["remaining_trades"],
                            "top3_removed_pnl": ex3["remaining_pnl"],
                            "top3_removed_avg_ret": ex3["remaining_avg_ret"],
                            "top3_removed_win_rate": ex3["remaining_win_rate"],
                        }
                    )

    summary = pd.DataFrame(summary_rows)
    windows = pd.DataFrame(window_rows)
    coverage = pd.DataFrame(coverage_rows)
    concentration = pd.DataFrame(concentration_rows)
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "window_metrics.csv", index=False, encoding="utf-8-sig")
    coverage.to_csv(OUT_DIR / "coverage.csv", index=False, encoding="utf-8-sig")
    concentration.to_csv(OUT_DIR / "concentration.csv", index=False, encoding="utf-8-sig")

    pct_cols = {
        "raw_avg_5d",
        "raw_win_rate",
        "total_return",
        "max_drawdown",
        "win_rate",
        "avg_trade_return",
        "worst_trade",
        "return",
        "top1_removed_avg_ret",
        "top1_removed_win_rate",
        "top3_removed_avg_ret",
        "top3_removed_win_rate",
    }
    report = "\n".join(
        [
            "# G3 横盘二次承接结构否决大样本复验 v1",
            "",
            "## 回测范围",
            f"- 候选信号/入场窗口：{BACKTEST_START} 至 {BACKTEST_END}。",
            f"- 资金曲线结算至：{CURVE_END}。",
            "- 复验对象：`deep_and_reclaim_d1_second_accept` 与 `deep_and_reclaim_any_second_accept`。",
            "- 本轮不是调参，只验证上一轮小样本复盘得到的结构否决是否能迁移到更大候选源。",
            "",
            "## 策略名解释",
            "- `deep_and_reclaim_d1_second_accept`：箱体底部且日线修复，并要求D1再次出现30m放量承接。",
            "- `deep_and_reclaim_any_second_accept`：箱体底部且日线修复，D0后半日或D1任一出现30m二次承接即可。",
            "- `not_floor_not_strong_veto`：否决既不够贴近60日底部，也没有日线强修复的样本。",
            "- `not_floor_not_strong_unless_extreme_volume`：否决既不够低也不够强，但如果30m极端放量承接则保留。",
            "",
            "## 覆盖率",
            md_table(coverage, pct_cols=pct_cols),
            "",
            "## 汇总结果",
            md_table(summary, pct_cols=pct_cols),
            "",
            "## 分窗口结果",
            md_table(windows, pct_cols=pct_cols),
            "",
            "## 集中度",
            md_table(concentration, pct_cols=pct_cols),
            "",
            "## 判断口径",
            "- 如果规则减少交易后，2024-2025/2026窗口更稳，并且 shock2_cost30 没有显著恶化，才说明否决有迁移价值。",
            "- 如果只是在小样本里删掉失败，但大样本收益/回撤没有改善，则只能保留为复盘观察项。",
        ]
    )
    (OUT_DIR / "REPORT.md").write_text(report + "\n", encoding="utf-8")
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()

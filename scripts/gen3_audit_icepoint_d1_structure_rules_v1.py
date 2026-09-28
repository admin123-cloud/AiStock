from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_test_range_box_stress_icepoint_climax_overlay_v1 import md_table  # noqa: E402


SOURCE = ROOT / "reports" / "gen3_icepoint_d1_second_accept_concentration_audit_v1" / "icepoint_d1_overlay_trades_enriched.csv"
OUT_DIR = ROOT / "reports" / "gen3_icepoint_d1_structure_rules_audit_v1"

BACKTEST_START = "2020-01-01"
BACKTEST_END = "2026-05-29"
CURVE_END = "2026-06-04"


RULES = [
    {
        "rule": "base_icepoint_d1",
        "desc": "冰点+D1二次承接全部样本",
    },
    {
        "rule": "near_floor_only",
        "desc": "只保留贴近60日箱体底部，range_pos60<=3%",
    },
    {
        "rule": "strong_daily_reclaim",
        "desc": "只保留日线收盘修复较强，close_position>=75%",
    },
    {
        "rule": "clear_30m_accept",
        "desc": "只保留30m承接量明显，amount_ratio3>=1.5",
    },
    {
        "rule": "two_of_three_structure",
        "desc": "够低、日线强修复、30m量承接三项中至少满足两项",
    },
    {
        "rule": "not_floor_not_strong_veto",
        "desc": "否决既不够低也不够强：剔除 range_pos60>3% 且 close_position<75%",
    },
    {
        "rule": "not_floor_not_strong_unless_extreme_volume",
        "desc": "否决既不够低也不够强，但允许30m极端放量 amount_ratio3>=3",
    },
]


def pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def load_trades() -> pd.DataFrame:
    d = pd.read_csv(SOURCE, low_memory=False)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    for col in [
        "policy_net_ret",
        "realized_pnl",
        "range_pos60",
        "close_position",
        "amount_ratio3",
        "gap_open",
        "index_mom20",
    ]:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    d["year"] = d["entry_date"].dt.year
    d["near_floor"] = d["range_pos60"].le(0.03)
    d["strong_reclaim"] = d["close_position"].ge(0.75)
    d["clear_accept"] = d["amount_ratio3"].ge(1.5)
    d["extreme_accept"] = d["amount_ratio3"].ge(3.0)
    d["structure_score"] = d[["near_floor", "strong_reclaim", "clear_accept"]].sum(axis=1)
    d["win"] = d["policy_net_ret"].gt(0)
    return d


def apply_rule(d: pd.DataFrame, rule: str) -> pd.DataFrame:
    if rule == "base_icepoint_d1":
        return d.copy()
    if rule == "near_floor_only":
        return d[d["near_floor"]].copy()
    if rule == "strong_daily_reclaim":
        return d[d["strong_reclaim"]].copy()
    if rule == "clear_30m_accept":
        return d[d["clear_accept"]].copy()
    if rule == "two_of_three_structure":
        return d[d["structure_score"].ge(2)].copy()
    if rule == "not_floor_not_strong_veto":
        return d[~((~d["near_floor"]) & (~d["strong_reclaim"]))].copy()
    if rule == "not_floor_not_strong_unless_extreme_volume":
        bad = (~d["near_floor"]) & (~d["strong_reclaim"]) & (~d["extreme_accept"])
        return d[~bad].copy()
    raise ValueError(rule)


def top_exclusion(part: pd.DataFrame, n: int) -> tuple[float, float, int]:
    if part.empty:
        return 0.0, 0.0, 0
    tail = part.sort_values("realized_pnl", ascending=False).iloc[n:].copy()
    return float(tail["realized_pnl"].sum()), float(tail["policy_net_ret"].mean()) if len(tail) else 0.0, int(len(tail))


def summarize_rule(base: pd.DataFrame, spec: dict[str, str]) -> dict[str, Any]:
    part = apply_rule(base, spec["rule"])
    pnl_ex_top1, avg_ex_top1, n_ex_top1 = top_exclusion(part, 1)
    pnl_ex_top3, avg_ex_top3, n_ex_top3 = top_exclusion(part, 3)
    return {
        "rule": spec["rule"],
        "desc": spec["desc"],
        "trade_count": int(len(part)),
        "pnl": float(part["realized_pnl"].sum()),
        "sum_trade_ret": float(part["policy_net_ret"].sum()),
        "avg_trade_ret": float(part["policy_net_ret"].mean()) if len(part) else 0.0,
        "win_rate": float(part["win"].mean()) if len(part) else 0.0,
        "worst_trade": float(part["policy_net_ret"].min()) if len(part) else 0.0,
        "top1_removed_remaining_trades": n_ex_top1,
        "top1_removed_pnl": pnl_ex_top1,
        "top1_removed_avg_trade_ret": avg_ex_top1,
        "top3_removed_remaining_trades": n_ex_top3,
        "top3_removed_pnl": pnl_ex_top3,
        "top3_removed_avg_trade_ret": avg_ex_top3,
    }


def yearly_by_rule(base: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for spec in RULES:
        part = apply_rule(base, spec["rule"])
        for year, g in part.groupby("year"):
            rows.append(
                {
                    "rule": spec["rule"],
                    "year": int(year),
                    "trade_count": int(len(g)),
                    "pnl": float(g["realized_pnl"].sum()),
                    "avg_trade_ret": float(g["policy_net_ret"].mean()),
                    "win_rate": float(g["win"].mean()),
                }
            )
    return pd.DataFrame(rows)


def trade_membership(base: pd.DataFrame) -> pd.DataFrame:
    d = base.copy()
    for spec in RULES:
        d[spec["rule"]] = False
        idx = apply_rule(base, spec["rule"]).index
        d.loc[idx, spec["rule"]] = True
    cols = [
        "entry_date",
        "code",
        "name",
        "policy_net_ret",
        "realized_pnl",
        "range_pos60",
        "close_position",
        "amount_ratio3",
        "near_floor",
        "strong_reclaim",
        "clear_accept",
        "structure_score",
        "not_floor_not_strong_veto",
        "not_floor_not_strong_unless_extreme_volume",
    ]
    return d[[c for c in cols if c in d.columns]].sort_values("realized_pnl", ascending=False)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = load_trades()
    summary = pd.DataFrame([summarize_rule(base, spec) for spec in RULES])
    yearly = yearly_by_rule(base)
    membership = trade_membership(base)
    summary.to_csv(OUT_DIR / "structure_rule_summary.csv", index=False, encoding="utf-8-sig")
    yearly.to_csv(OUT_DIR / "structure_rule_yearly.csv", index=False, encoding="utf-8-sig")
    membership.to_csv(OUT_DIR / "structure_rule_trade_membership.csv", index=False, encoding="utf-8-sig")

    pct_cols = {
        "sum_trade_ret",
        "avg_trade_ret",
        "win_rate",
        "worst_trade",
        "top1_removed_avg_trade_ret",
        "top3_removed_avg_trade_ret",
        "policy_net_ret",
        "range_pos60",
        "close_position",
        "amount_ratio3",
    }
    best = summary[summary["rule"].eq("not_floor_not_strong_unless_extreme_volume")].iloc[0].to_dict()
    report = "\n".join(
        [
            "# G3 冰点+D1二次承接结构规则审计 v1",
            "",
            "## 回测范围",
            f"- 候选信号/入场窗口：{BACKTEST_START} 至 {BACKTEST_END}。",
            f"- 资金曲线结算至：{CURVE_END}。",
            "- 审计对象：`icepoint_d1_second_accept`，中文意思是“冰点环境下，箱体底部日线修复后，D1再次出现30m放量承接”。",
            "- 本轮只做结构复盘，不把规则升级为正式买点。",
            "",
            "## 结构名解释",
            "- `near_floor_only`：只买非常贴近60日箱体底部的样本，`range_pos60<=3%`。",
            "- `strong_daily_reclaim`：只买日线收盘修复较强的样本，`close_position>=75%`。",
            "- `clear_30m_accept`：只买30m承接量明显的样本，`amount_ratio3>=1.5`。",
            "- `two_of_three_structure`：够低、日线强修复、30m量承接三项至少满足两项。",
            "- `not_floor_not_strong_veto`：否决既不够低也不够强的样本。",
            "- `not_floor_not_strong_unless_extreme_volume`：否决既不够低也不够强，但允许30m极端放量承接。",
            "",
            "## 规则结果",
            md_table(summary, pct_cols=pct_cols),
            "",
            "## 分年度结果",
            md_table(yearly, pct_cols=pct_cols),
            "",
            "## 逐笔归属",
            md_table(membership, pct_cols=pct_cols),
            "",
            "## 阶段判断",
            f"- `not_floor_not_strong_unless_extreme_volume` 保留 {int(best['trade_count'])} 笔，Pnl {best['pnl']:.2f}，胜率 {pct(best['win_rate'])}，最差单笔 {pct(best['worst_trade'])}。",
            "- 这类规则的作用更像失败样本否决，而不是新买点；样本太少，不能继续微调阈值。",
            "- 下一步应把这条否决逻辑放回更大的候选源上复验，看它是否能在不严重牺牲收益的情况下减少失败样本。",
        ]
    )
    (OUT_DIR / "REPORT.md").write_text(report + "\n", encoding="utf-8")
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()
